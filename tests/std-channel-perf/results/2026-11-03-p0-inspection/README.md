# P0 offline ESP32-S3 image attribution

This is **offline analysis of existing physical-run artifacts**, not a new board
run. The original November 2 captures and their checksum manifest are unchanged.

Inputs: GitHub Actions artifact **13198576947** from run **41561882690**, archive
SHA-256 `ccb825d9b83d0d7727770fb1596eead6839728d345e7840723caf6c46d71b2aa`.
Each report verifies the complete 16 MiB merged image against its build receipt
and matches both printed ROM values in the cited original raw console log.

| Image | SHA-256 of bytes through RAM checksum at offset `0xc060` |
| --- | --- |
| latency | `1f9ac51242b748b9debf101e8b85b7942292474875180f1bc54ccf7a0c3e0c63` |
| reserved witness | `5d4b69da51bc0cb89b343e3ccb5fded770617c0a7040ebf7549a215db5bccbbe` |
| published witness | `8389f013277f49cf33f0216eecf3eeaa28b1f12b759e29619ffaebd1a6894fd6` |

All three header segment counts are three and all three append-digest flags are
zero. The next 32 bytes are
`00000000903f0000000000000000000000000000000000000000000000000000`.
They exactly reproduce the ROM's `Expected` line: address-zero padding header,
size `0x3f90`, then padding bytes. The remaining image contains aligned mapped
segments that the initial ROM segment count excludes.

The build log identifies esptool **4.8.1** and `elf2image --ram-only-header`.
The pinned [image writer](https://github.com/espressif/esptool/blob/v4.8.1/esptool/bin_image.py)
writes that RAM/checksum/hidden-flash layout. Thus the captured warning can be
reproduced from image-generation semantics without assuming a bad transfer.
This does **not** establish that every ROM warning is harmless, resolve the
warning with a fresh boot, or authorize bypassing the timing gate.

To reproduce with the extracted CI artifact and an unchanged archived log:

```sh
python3 tests/std-channel-perf/inspect_esp_image.py \
  --build-directory /path/to/artifact/esp32s3/latency \
  --transcript tests/std-channel-perf/results/2026-11-02-esp32s3/runs/latency-1/console.log \
  --output /path/to/new-diagnostic.json
```

The three JSON reports record exact image/log hashes, offsets and segment
boundaries. `boot_warning_resolved` and `physical_timing_qualified` remain false.
The reports add artifact identity and original capture paths to the CLI output.
