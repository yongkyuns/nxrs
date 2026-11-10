# PR #257 — physical WROOM SIMPLE_BOOT comparison

The paired `ram-prefix-digest-v1` image and rebuilt loader booted without the
ROM SHA-256 warning on four fresh resets. The separately built legacy-packing
control reproduced the warning on the same board. All five commands returned
valid results and matched their flashed image's runtime build identity.

The connected ESP32-S3 N16R8 board identifies as QFN56 revision v0.2, USB/MAC
`esp32s3-bench-01`, 16 MiB flash and 8 MiB embedded octal PSRAM. The fixture uses
one configured 240 MHz core, PM disabled, 16 KiB instruction / 32 KiB data cache,
and PSRAM in the common heap. No camera or radio application was running.
These observations qualify this board and USB-reset boot path only.

| Case | Retained samples | ROM warning | Receive p50 / p99 / maximum cycles | Send-call maximum cycles |
| --- | ---: | --- | --- | ---: |
| Legacy packing, tight | 1,000 | Present | 1866 / 1873 / 3581 | 5901 |
| Digest candidate, tight 1 | 1,000 | Absent | 1866 / 1873 / 3686 | 5395 |
| Digest candidate, tight 2 | 1,000 | Absent | 1866 / 1873 / 3511 | 5227 |
| Digest candidate, tight 3 | 1,000 | Absent | 1866 / 1873 / 3683 | 5392 |
| Digest candidate, seeded delay 1 | 1,000 | Absent | 2997 / 3673 / 5232 | 13268 |

Each boot excluded 100 warmups, independently verified every retained receiver
blocked in NuttX, verified terminal receiver quiescence, validated payloads and
joined workers. The seed-1 schedule passed replay validation; it changes setup
activity and remains a separate diagnostic distribution. It does not establish
IRQ phase coverage, representative offered load, CPU utilization or power.
Send-call time includes preempting receiver execution. No overhead is subtracted.

**Absolute timing remains unqualified:** no independent clock-reference capture
was available. Every official capture completed while retaining
`physical_timing_qualified: false`; candidate runs report only
`clock-not-independently-calibrated`, while the legacy control also reports
`rom-digest-warning`. Exact channel/stack allocation placement was not measured.
There is no performance-improvement, WCET or cross-RTOS ranking claim.

## Reproduction and provenance

Firmware came from the successful [PR-head workflow](https://github.com/yongkyuns/nxrs/actions/runs/41679548116),
[artifact 13254836111](https://github.com/yongkyuns/nxrs/actions/runs/41679548116/artifacts/13254836111).
Its ZIP SHA-256 is `398301709b37b932e3b3aa0eea97f4f505862e9d2213c0781462dc32235dbec9`.
The CI checkout was merge revision `cbc9dd4327958194de2ec0f93046d0f3605177fe`;
all 127 recorded firmware-input hashes match PR head
`4a0f47e750e8fcdbc2a42c2195c4a93b02c14952`. All recorded artifact hashes were
verified, including the unchanged source image and paired loader/packing receipt.
The legacy control is a separate build, not a byte-identical executable pair for
attributing timing differences.

`provenance.json` identifies images and capture sources; `firmware-receipts/`
retains build, configuration, patch and packing records. `runs/` retains complete
raw boot/command transcripts, sessions and official analyses. Re-run
`analyze.analyze()` on each transcript to reproduce its sample statistics;
`observations.json` keeps the distributions separate. Firmware binaries remain
in the CI artifact. The ROM-warning observation does not authenticate the hidden
mapped payloads or establish secure boot; the board reports secure boot and flash
encryption disabled.

The fresh pre-test full flash was backed up privately, verified, then restored
and verified after testing. Older snapshots were not used. Only backup metadata
and tool receipts are retained here; the backup binary and its credentials are
excluded. `run-physical.py` records the operator used, with host-local paths;
future tests require a new backup and output directory. `SHA256SUMS` covers every
retained file except itself and preserves raw serial bytes.

Local validation: 82 fixture tests, four native original/patched loader controls,
the 52-package architecture audit, 23 architecture regressions, 10 HAL-boundary
regressions and three documentation tests passed. The PR-head std-channel CI
workflow passed; its separate secrets-scanning job failed and is not resolved by
these physical results. PR #257 remains draft; independent calibration and
placement/resource qualification under #252 remain open.

## Privacy of this public copy

The physical board MAC/USB serial is represented by the stable pseudonym
`esp32s3-bench-01`; the host serial-device path is `<serial-device>`. These labels
preserve the relationship between this board's runs without publishing its
hardware identifier. They are not connection settings for a new measurement.

`privacy-redaction.json` records the original and public-copy file digests.
`SHA256SUMS` and cohort evidence references describe the sanitized files.
Firmware/build/source identities, console measurements, numeric results and
qualification outcomes are unchanged. This is a publication edit, not a new
hardware run or a rewrite of Git history.
