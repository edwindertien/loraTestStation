One subfolder per firmware role, each holding the single merged
(bootloader+partitions+app) `.bin` for that role, e.g.:

```
firmware_bins/
  meshtastic/firmware.bin
  meshcore/firmware.bin
  lora_node/firmware.bin
  sc_gateway/firmware.bin
```

The dashboard (and flash_role.sh) picks the first `*.bin` it finds in each
role's folder, so keep exactly one there at a time. See the top-level
README.md for how to produce this file for each role.
