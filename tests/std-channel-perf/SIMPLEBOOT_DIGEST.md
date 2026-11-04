# P0: paired SIMPLE_BOOT digest candidate

This is an opt-in build candidate for nxrs #252, not a qualified physical fix,
secure-boot implementation, or channel optimization. The historical evidence and
all existing boot-warning, clock-calibration and progress gates remain unchanged.

## Image and loader must agree

The pinned ESP32-S3 SIMPLE_BOOT image contains ROM-visible RAM segments and an
aligned XOR checksum, followed by hidden padding and mapped code/data segments.
The retained ROM warning compares the RAM-prefix SHA-256 with bytes occupied by
the first hidden padding header, rather than a stored digest. Why the observed
ROM checks this when the original header declares no appended digest remains
unresolved. Byte attribution alone is not proof that a candidate boots correctly.

Patch `0107-xtensa-simpleboot-hash-offset.patch` teaches the pinned loader to skip
a declared 32-byte digest before parsing hidden segments. Legacy flag-zero
images follow the unchanged path. This is parser support, not digest checking.

`simpleboot_digest.py` provides the explicit `ram-prefix-digest-v1` profile. It
validates the existing RAM checksum and supported segment layout, consumes 32
bytes of existing zero padding, declares and writes the prefix digest, and checks
that RAM payloads, mapped payloads, mapped offsets and total length are unchanged.
The source image is never replaced. The candidate and its JSON receipt are
created exclusively under separately named paths.

Generation requires the exact pinned loader source and the applied patch's
before/after hashes and provenance ledger from the newly completed private
build. Old archived images cannot be relabeled as deployable candidates. A pure
byte-repacking test does not establish that the embedded executable supports it.
The selected profile participates in the runtime build identity; the receipt
hashes the original image, candidate, ELF, configuration, patch ledger and report.

The prefix digest does **not** cover the hidden mapped payloads and is not a
whole-firmware integrity or authenticity guarantee. Whole-image artifact hashes
remain necessary. Do not suppress a ROM warning because an offline digest passes.

## Build and test

Use the repository's pinned SDK and a fresh output directory:

```sh
python3 tests/std-channel-perf/build.py \
  --platform esp32s3-service-footprint --mode latency \
  --clock-source native-counter --boot-image-profile ram-prefix-digest-v1 \
  --out target/std-channel-perf/esp32s3/latency-rom-digest
```

The default is still `platform-default`, with original packing. The loader patch
is registered in the normal isolated NuttX patch series, so every rebuilt image
has a new source identity even when its packing stays unchanged.

```sh
python3 -m unittest discover -s tests/std-channel-perf -p 'test_*.py' -v
python3 tests/nuttx-std/test-simpleboot-digest.py -v
```

The native test needs the exact NuttX submodule and a C compiler. It compiles the
actual pinned original and patched parsing blocks with bounded flash-read stubs.
The original parser must reject the new layout; the patched parser must accept
both layouts. These tests are not a complete Xtensa translation-unit build, SDK
ABI check, ROM execution, or hardware boot.

CI retains the existing Rust, ARM/QEMU, Pico 2 and three ESP32-S3 cases, adds the
native parser controls, then builds a separately named ESP32-S3 digest candidate.
It retains `nuttx.rom-digest.bin`, its original image, ELF, full build log, runtime
build identity and packing/patch receipts. No workflow step flashes a device.

## Acceptance still required

A successful full target build establishes compile/link and image/provenance
consistency only. Before closing this P0 gate, retain a fresh physical boot with
matching flashed-image/runtime identities and **no** ROM digest warning. Then
qualify independent clock calibration, corrected tight/seeded sampling, placement
and resource observations separately. Do not overwrite or discard the old failed
captures. P0 remains open until the physical acceptance criteria pass.
