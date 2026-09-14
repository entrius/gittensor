# gt-controller

`gitt controller` in its own container (vault `26` §1, §3, §8). Outbound SSH only: no docker socket, no ports, no
chain key. The CA private key, the state directory and the GPU-proof provider are mounts.

```bash
docker build -f docker/controller/Dockerfile --build-arg EXTRA_PIP_PACKAGES="pynacl==1.6.0" \
    -t entrius/gt-controller:dev .
```

| Mount | In the container | What |
|---|---|---|
| state dir (owned by `--user`) | `/state` | `boxes.json`, `known_hosts`, `nvml_allowlist.json` |
| SSH CA private key | `/secrets/gt_ca` (read-only) | signs the ~5-minute per-visit certificates |
| proof provider source | `/opt/proof` (read-only, on `PYTHONPATH`) | the private `GpuProof` implementation |
| proof binary + secret store | `/opt/proof-dist` (read-only) | e.g. `gt_proof`, `gt_proof.version`, `secret_store.json` |

```bash
docker run --rm --user "$(id -u):$(id -g)" \
    -v ~/.gittensor/controller:/state \
    -v /path/to/gt_ca:/secrets/gt_ca:ro \
    -v /path/to/provider:/opt/proof:ro -e PYTHONPATH=/opt/proof \
    -v /path/to/dist:/opt/proof-dist:ro \
    entrius/gt-controller:dev \
    check <hotkey> --state-dir /state --ca-key /secrets/gt_ca \
        --agent-image-digest sha256:<published agent digest> \
        --proof <module:Class> \
        --proof-args secret_store=/opt/proof-dist/secret_store.json \
        --proof-args version=@/opt/proof-dist/gt_proof.version \
        --proof-args binary_path=/opt/proof-dist/gt_proof
```

The same flags drive `round` (add `--loop` for the 20-minute cycle). `admit <hotkey> --host <ip> --port <port>` and
`allowlist add <hotkey>` come first; see `gitt controller --help`. A per-round proof build needs the private build
pipeline, which this image deliberately does not carry: run it where that lives and point `--build-cmd` at it, or
rebuild outside and let each round re-read the mounted `dist`.
