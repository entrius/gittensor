# AMD fixtures (synthetic)

What `AMD_SYSFS_COMMAND` (`checks/amd_scrape.py`) and `AMD_DEVICE_HOLDERS_COMMAND` print on an MI300X box, **written
from the kernel's sysfs formats, not captured from a card** (10/8): the DRM `unique_id` is `%016llx`, the KFD topology
`unique_id` the same number in decimal, `gfx_target_version` 90402 is gfx942, hwmon `power1_cap` is microwatts,
`mem_info_vram_total` is bytes (192 GiB). The first MI300X run (vault 31 step 1 item 1) replaces these with a real
capture; until then every number here is an assumption the tests hold the parsers to, nothing more.
