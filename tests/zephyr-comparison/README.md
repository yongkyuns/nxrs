# Historical NuttX and Zephyr comparison

Isolated ESP32-S3 experiment; not a production port.
See [RTOS analysis](../../docs/rtos-comparison.md) for current conclusions.
This guide preserves the pinned Zephyr setup and historical
producer/worker/collector workload. The newer
[independent-service experiment](../event-services-comparison/README.md) is
different; do not combine measurements. See the separate [Embassy guide](../embassy-comparison/README.md)
for this workload with cooperative Rust tasks.

## Fixture and provenance

Four producers, 15 workers and one collector exchange 5,760 event/reply pairs
through 60 bounded queues and 20 threads. Input queues hold one 248-byte event;
reply queues hold four. Workers wait on three inputs, the collector on 15
outputs; ready queues are selected round-robin and full sends block. Wire mode
checks routing, sequence, order and checksum. Packet mode filters a 200-byte
packet of 32 XYZ frames and checks each result. Every run validates 5,760
replies and digest `441445568`. Timing spans the start gate to final
validation, includes backpressure, and excludes setup and console output.

The measured Zephyr source is v4.3.1 at
`75f67d766726351b30199f9a2bf55803d717a3be`. Its `hal_espressif` module is
`af6cfa2e3e7098b596062ab516b80a48a7ba7332`; `hal_xtensa` is
`3cc9e3a9360be5c96c956dce84064b85439b6769`. The board is a single-core
240 MHz Freenove ESP32-S3-WROOM. Matched cases use DIO 40 MHz flash, 16/32 KiB
instruction/data caches, 100 Hz scheduling, checking-enabled kernels,
83,968 bytes of spawned stacks and internal SRAM. Zephyr uses native
`k_msgq`, `k_poll` and `k_thread`, not POSIX compatibility. Console/platform
features differ from NuttX. Compiler, 80 MHz flash
and lean-check cases are separate sensitivity cohorts.

The [frozen evidence](results/esp32s3-2026-10-03.json) stores run values,
configuration/artifact hashes and matrix order. The report checks firmware
inputs and regenerated `matched_control.c`; historical tool hashes are not a
complete rebuild claim.

## Reproduce locally

Use CMake, Ninja, dtc, Python 3.10+, Zephyr SDK 0.17.0 and its ESP32-S3
Xtensa toolchain, plus the pinned NuttX/ESP Rust environment. Initialize an
isolated west workspace at the pinned revisions, update only the two modules,
and install Zephyr base requirements plus `esptool==5.3.0` locally. Helpers
verify revisions and hardware settings. Keep checkouts external or in ignored
`target/`; use fresh output directories and do not patch Zephyr.

```sh
python3 -m unittest discover -s tests/zephyr-comparison -p 'test_*.py' -v
python3 tests/service-footprint/check_native.py
python3 tests/zephyr-comparison/build.py \
  --zephyr "$ZEPHYR_SRC" --espressif "$ESPRESSIF_HAL" --xtensa "$XTENSA_HAL" \
  --sdk "$ZEPHYR_SDK" --python "$ZEPHYR_PYTHON" \
  --mode packet --profile speed --out target/zephyr-matched-packet-2
```

Build wire, size and baseline cases separately. Before flashing, make a
private full 16 MiB backup, verify length and SHA-256, and `chmod 600` it.
Firmware may contain credentials; never upload or commit the backup.
`run_matrix.py` verifies images and restores/verifies the backup in `finally`.
If restoration fails, stop and restore manually. Keep exclusive board access.

```sh
python3 tests/zephyr-comparison/run_matrix.py \
  --artifacts target --out target/packet-matrix \
  --backup "$PRIVATE_BACKUP" --port "$DEVICE_PORT" --flasher "$ESPTOOL_V4" \
  --case c-packet-2 --case rust-packet-2 --case zephyr-packet-2 \
  --runs 10 --blocks 3
python3 tests/zephyr-comparison/report.py --artifacts target \
  --matrix target/packet-matrix --readelf "$ESP_READ_ELF" \
  --nuttx-compiler "$ESP_GCC" --out target/comparison-results.json
```
