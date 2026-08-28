// gt_attest — hardware attestation challenge for serving miners (Gittensor compute).
//
// Fills the GPU's free VRAM with a seeded stream, then runs a seeded chain of fp32 matrix products through tanh over a
// working set carved from that fill, hashing the first 64 KiB of every matrix after each iteration into one SHA-256
// digest. Deterministic for a given seed on any sm_120 card (hand-written tiled GEMM, no atomics, no reductions, no
// TF32, no fast-math), so a validator's reference 5090 recomputes the same digest. One pass (--iters 3) is sized to
// ~1.5 s on an idle 5090; two hotkeys sharing a card cannot both fill the free VRAM, and their chains run ~2x slower.
//
// Every device answers its own seed — the challenge seed plus the device index — and all devices run at the same
// time, each on its own thread. A box with N cards therefore finishes in one card's wall time with N distinct
// digests, while one card impersonating N has to run the chain N times in a row: the validator recomputes each
// index's digest on its reference and holds the whole reply to one card's round trip.
//
//   gt_attest --seed <u64> [--iters 3] [--fill] [--device <i>|all] [--dim 1024] [--matrices 512]
// prints one JSON object (or {"devices":[...]} for all) and exits 0; exit 2 = bad args, 3 = allocation failure.
#include <cuda_runtime.h>
#include <nvml.h>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <chrono>
#include <string>
#include <thread>
#include <vector>

#define CHECK(x) do { cudaError_t e_ = (x); if (e_ != cudaSuccess) { std::fprintf(stderr, "{\"error\":\"%s at %s:%d\"}\n", cudaGetErrorString(e_), __FILE__, __LINE__); std::exit(3); } } while (0)

// ---------------------------------------------------------------- SHA-256 (host) --------------------------------
struct Sha256 {
    uint32_t h[8]; uint64_t len = 0; uint8_t buf[64]; size_t n = 0;
    static uint32_t rotr(uint32_t x, int k) { return (x >> k) | (x << (32 - k)); }
    Sha256() { const uint32_t iv[8] = {0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19}; std::memcpy(h, iv, 32); }
    void block(const uint8_t* p) {
        static const uint32_t K[64] = {0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
        uint32_t w[64];
        for (int i = 0; i < 16; i++) w[i] = (uint32_t)p[4*i] << 24 | (uint32_t)p[4*i+1] << 16 | (uint32_t)p[4*i+2] << 8 | p[4*i+3];
        for (int i = 16; i < 64; i++) { uint32_t s0 = rotr(w[i-15],7) ^ rotr(w[i-15],18) ^ (w[i-15] >> 3); uint32_t s1 = rotr(w[i-2],17) ^ rotr(w[i-2],19) ^ (w[i-2] >> 10); w[i] = w[i-16] + s0 + w[i-7] + s1; }
        uint32_t a=h[0],b=h[1],c=h[2],d=h[3],e=h[4],f=h[5],g=h[6],hh=h[7];
        for (int i = 0; i < 64; i++) { uint32_t S1 = rotr(e,6)^rotr(e,11)^rotr(e,25); uint32_t ch = (e&f)^(~e&g); uint32_t t1 = hh+S1+ch+K[i]+w[i]; uint32_t S0 = rotr(a,2)^rotr(a,13)^rotr(a,22); uint32_t mj = (a&b)^(a&c)^(b&c); uint32_t t2 = S0+mj; hh=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2; }
        h[0]+=a; h[1]+=b; h[2]+=c; h[3]+=d; h[4]+=e; h[5]+=f; h[6]+=g; h[7]+=hh;
    }
    void update(const uint8_t* p, size_t k) { len += k; while (k) { size_t t = 64 - n; if (t > k) t = k; std::memcpy(buf + n, p, t); n += t; p += t; k -= t; if (n == 64) { block(buf); n = 0; } } }
    std::string hex() { uint64_t bits = len * 8; uint8_t pad = 0x80; update(&pad, 1); uint8_t z = 0; while (n != 56) update(&z, 1); uint8_t l[8]; for (int i = 0; i < 8; i++) l[i] = (uint8_t)(bits >> (56 - 8*i)); update(l, 8); char out[65]; for (int i = 0; i < 8; i++) std::snprintf(out + 8*i, 9, "%08x", h[i]); return std::string(out, 64); }
};

// ---------------------------------------------------------------- kernels ---------------------------------------
__device__ __forceinline__ uint64_t splitmix(uint64_t x) { x += 0x9e3779b97f4a7c15ULL; x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL; x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL; return x ^ (x >> 31); }

// Seeded fill: every element depends only on (seed, index) — deterministic and embarrassingly parallel.
__global__ void fill_kernel(float* p, size_t n, uint64_t seed) {
    size_t i = (size_t)blockIdx.x * blockDim.x + threadIdx.x; size_t stride = (size_t)gridDim.x * blockDim.x;
    for (; i < n; i += stride) { uint64_t r = splitmix(seed ^ (i * 0x2545F4914F6CDD1DULL)); p[i] = ((r >> 40) & 0xFFFFFF) / 16777216.0f - 0.5f; }
}

// C = tanh(A * B) for square d x d fp32 matrices; 32x32 tiles, fixed summation order per element, no FMA contraction
// (compiled with -fmad=false) so the result is bit-identical on every card of the architecture.
#define TILE 32
__global__ void gemm_tanh_kernel(const float* __restrict__ A, const float* __restrict__ B, float* __restrict__ C, int d) {
    __shared__ float As[TILE][TILE]; __shared__ float Bs[TILE][TILE];
    int row = blockIdx.y * TILE + threadIdx.y, col = blockIdx.x * TILE + threadIdx.x; float acc = 0.0f;
    for (int t = 0; t < d; t += TILE) {
        As[threadIdx.y][threadIdx.x] = A[(size_t)row * d + t + threadIdx.x];
        Bs[threadIdx.y][threadIdx.x] = B[(size_t)(t + threadIdx.y) * d + col];
        __syncthreads();
        #pragma unroll
        for (int k = 0; k < TILE; k++) acc = acc + As[threadIdx.y][k] * Bs[k][threadIdx.x];
        __syncthreads();
    }
    C[(size_t)row * d + col] = tanhf(acc * (1.0f / 64.0f));
}

// ---------------------------------------------------------------- one device ------------------------------------
static std::string device_uuid(int dev) {
    char pci[32]; if (cudaDeviceGetPCIBusId(pci, sizeof pci, dev) == cudaSuccess) {
        nvmlDevice_t h; char uuid[NVML_DEVICE_UUID_V2_BUFFER_SIZE];
        if (nvmlDeviceGetHandleByPciBusId_v2(pci, &h) == NVML_SUCCESS && nvmlDeviceGetUUID(h, uuid, sizeof uuid) == NVML_SUCCESS) return uuid;
    }
    cudaDeviceProp p; cudaGetDeviceProperties(&p, dev); char out[64]; std::snprintf(out, sizeof out, "GPU-");
    for (int i = 0; i < 16; i++) std::snprintf(out + 4 + 2*i, 3, "%02x", (unsigned char)p.uuid.bytes[i]);
    return out;
}

static std::string run_device(int dev, uint64_t challenge_seed, int iters, bool fill, int d, int matrices) {
    auto t0 = std::chrono::steady_clock::now();
    uint64_t seed = challenge_seed + (uint64_t)dev;  // per-device seed: index i answers seed + i
    CHECK(cudaSetDevice(dev));
    cudaDeviceProp prop; CHECK(cudaGetDeviceProperties(&prop, dev));
    size_t free_b = 0, total_b = 0; CHECK(cudaMemGetInfo(&free_b, &total_b));
    size_t mat_bytes = (size_t)d * d * sizeof(float), work_bytes = mat_bytes * (size_t)(matrices + 1);
    if (free_b < work_bytes + (256u << 20)) { std::printf("{\"device\":%d,\"error\":\"not enough free VRAM: %zu\"}\n", dev, free_b); std::exit(3); }
    // fill: everything free minus headroom, in large chunks (the working set is the first chunk)
    std::vector<void*> chunks; size_t filled = 0;
    size_t want = fill ? free_b - (256u << 20) : work_bytes;
    while (filled < want) {
        size_t chunk = want - filled; if (chunk > (2ull << 30)) chunk = 2ull << 30; if (chunks.empty() && chunk < work_bytes) chunk = work_bytes;
        void* p = nullptr; if (cudaMalloc(&p, chunk) != cudaSuccess) { if (chunks.empty()) { std::printf("{\"device\":%d,\"error\":\"fill\"}\n", dev); std::exit(3); } break; }
        fill_kernel<<<4096, 256>>>((float*)p, chunk / sizeof(float), seed ^ (uint64_t)chunks.size());
        chunks.push_back(p); filled += chunk;
    }
    CHECK(cudaDeviceSynchronize());
    float* M = (float*)chunks[0]; float* scratch = M + (size_t)matrices * d * d;
    dim3 grid(d / TILE, d / TILE), block(TILE, TILE);
    Sha256 sha; std::vector<uint8_t> head(64 * 1024);
    for (int it = 0; it < iters; it++) {
        for (int i = 0; i < matrices; i++) {
            const float* A = M + (size_t)i * d * d; const float* B = M + (size_t)((i + 1) % matrices) * d * d;
            gemm_tanh_kernel<<<grid, block>>>(A, B, scratch, d);
            CHECK(cudaMemcpyAsync((void*)A, scratch, mat_bytes, cudaMemcpyDeviceToDevice));
        }
        CHECK(cudaDeviceSynchronize());
        for (int i = 0; i < matrices; i++) { CHECK(cudaMemcpy(head.data(), M + (size_t)i * d * d, head.size(), cudaMemcpyDeviceToHost)); sha.update(head.data(), head.size()); }
    }
    for (void* p : chunks) cudaFree(p);
    double ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
    char out[1024];
    std::snprintf(out, sizeof out, "{\"device\":%d,\"uuid\":\"%s\",\"name\":\"%s\",\"sm_count\":%d,\"vram_total\":%zu,\"vram_free_before\":%zu,\"filled_bytes\":%zu,\"dim\":%d,\"matrices\":%d,\"iters\":%d,\"digest\":\"%s\",\"wall_ms\":%.1f}",
        dev, device_uuid(dev).c_str(), prop.name, prop.multiProcessorCount, total_b, free_b, filled, d, matrices, iters, sha.hex().c_str(), ms);
    return std::string(out);
}

int main(int argc, char** argv) {
    uint64_t seed = 0; int iters = 3, d = 1024, matrices = 512; bool fill = false, have_seed = false; std::string device = "0";
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i]; auto next = [&](void) -> const char* { if (i + 1 >= argc) { std::fprintf(stderr, "missing value for %s\n", a.c_str()); std::exit(2); } return argv[++i]; };
        if (a == "--seed") { seed = std::strtoull(next(), nullptr, 10); have_seed = true; }
        else if (a == "--iters") iters = std::atoi(next());
        else if (a == "--dim") d = std::atoi(next());
        else if (a == "--matrices") matrices = std::atoi(next());
        else if (a == "--device") device = next();
        else if (a == "--fill") fill = true;
        else { std::fprintf(stderr, "unknown arg %s\n", a.c_str()); return 2; }
    }
    if (!have_seed || iters < 1 || d % TILE != 0 || d < TILE || matrices < 2) { std::fprintf(stderr, "usage: gt_attest --seed <u64> [--iters n] [--fill] [--device i|all] [--dim d] [--matrices m]\n"); return 2; }
    nvmlInit_v2();
    int count = 0; CHECK(cudaGetDeviceCount(&count));
    if (device == "all") {
        // every card at once, each on its own thread: N cards take one card's wall, one card faking N takes N
        std::vector<std::string> out(count); std::vector<std::thread> threads;
        for (int i = 0; i < count; i++) threads.emplace_back([&, i]() { out[i] = run_device(i, seed, iters, fill, d, matrices); });
        for (auto& t : threads) t.join();
        std::printf("{\"devices\":[");
        for (int i = 0; i < count; i++) std::printf("%s%s", out[i].c_str(), i == count - 1 ? "" : ",");
        std::printf("]}\n");
    }
    else { int dev = std::atoi(device.c_str()); if (dev < 0 || dev >= count) { std::fprintf(stderr, "no device %d\n", dev); return 2; } std::printf("%s\n", run_device(dev, seed, iters, fill, d, matrices).c_str()); }
    nvmlShutdown();
    return 0;
}
