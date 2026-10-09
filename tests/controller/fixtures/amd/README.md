# AMD fixtures

`sysfs_mi325x_1.txt` is **real**: `AMD_SYSFS_COMMAND` (`checks/amd_scrape.py`) captured by `capture.sh` on a
DigitalOcean 1x MI325X GPU Droplet on 2026-10-09 (Ubuntu 24.04.4, kernel 6.8.0-137-generic, amdgpu DKMS
6.19.14.31400000, ROCm 7.14 preview, whole-GPU passthrough). What it settled against the 10/8 assumptions:

| Field | Assumed | Real |
| ----- | ------- | ---- |
| PCI `device` of an MI325X | `0x74a5` | **`0x74b9`** (`0x74a5` is the subsystem id; both are on the catalog row) |
| `product_name` | a marketing string | **empty**; the name comes from the catalog row by PCI id |
| `unique_id` | 16 hex | 16 hex, non-zero, and the KFD node's decimal `unique_id` is the same number |
| `mem_info_vram_total` | 256 GiB exactly | 274542362624 B = 261824 MiB (0.3 % under the nominal 262144) |
| `power1_cap*` | 750 W (an MI300X) | 1000 W, cap = default = max |
| `/sys/module/amdgpu/version` | absent (in-tree) | present: the DKMS driver's version |
| `--group-add video --group-add render` | names | **fails** ("Unable to find group render": docker resolves names inside the container); the command now prints the host's gids, `video` 44 and `render` 992 here (the two `gid_*` lines were appended to the capture from `getent group` on the same box) |
| KFD nodes | one per card | node 0 is the CPU (all zeros); the card is node 1 |
| render nodes | `renderD128` is the card | `renderD128` is the virtio display (vendor 0x1af4, skipped); the card is `renderD129`; `renderD130`-`136` exist with no device files |

`sysfs_mi325x_8.txt` is **derived** from the real file: the same card eight times, serials `real + i`, render nodes
`renderD129`-`136`, one KFD node per card; the PCI addresses are invented. It holds the parsers to the eight-card
join (id -> KFD node -> render minor -> node); the first real 8x capture replaces it.

`device_holders_kfd_desktop.txt` is synthetic: `AMD_DEVICE_HOLDERS_COMMAND` on a box where a desktop session and a
foreign container hold `/dev/kfd` and the card's render node (the holder scan at rest on the droplet was empty, as
the heartbeat expects).
