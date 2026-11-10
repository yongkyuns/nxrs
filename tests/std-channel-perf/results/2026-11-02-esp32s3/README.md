# PR #252 — physical ESP32-S3 observations

ESP32-S3 rev v0.2, N16R8, one core; fixed 240 MHz configuration, PSRAM common heap,
16 KiB instruction / 32 KiB data cache. The [CI artifact](https://github.com/yongkyuns/nxrs/actions/runs/41561882690/artifacts/13198576947)
built merge revision `93856f7`; all 123 firmware input hashes match PR head
`3d89b16`. Firmware, ELF and resolved-configuration hashes were checked before
use. Each capture followed a hardware reset and matched its runtime build ID.

| Physical case | Observation |
| --- | --- |
| Blocked receiver, 3 fresh boots | 3,000/3,000 retained waits independently observed in native TCB state; 100 excluded warmups per boot; payloads valid |
| Reserved slot | Higher-priority consumer stalled until explicit priority recovery; valid delivery and joined workers |
| Published-slot control | Delivery and joins completed without recovery |

| Timing boundary | p50 cycles | p99 cycles | Observed max cycles | Conditional p50 / p99 / max at configured 240 MHz |
| --- | ---: | ---: | ---: | --- |
| Before send → receive return | 1864 | 1871 | 11958 | 7.767 / 7.796 / 49.825 µs |
| Send call | 3577 | 3584 | 215296 | 14.904 / 14.933 / 897.067 µs |

**Timing is unqualified:** an independent CPU-clock calibration was unavailable. The counter passed only the fixture's plausibility probe. Official latency captures retain their calibration rejection; separate analyses validate the raw samples. Send-call timing includes higher-priority receiver execution; these are neither pure context-switch costs nor WCET. The reserved-slot result is negative progress evidence. No cross-RTOS ranking is established.

The original runner rejected the target's NuttX startup banner because the optional NuttShell greeting was absent. The included host-only patch accepts either startup marker plus the prompt; missing-banner and missing-prompt controls remain rejecting. All 47 fixture tests, the architecture audit, 23 architecture regressions and 10 HAL-boundary regressions passed.

Boot transcripts retain a ROM image-digest warning despite successful full-image flash verification; its cause remains uninvestigated. Exact channel allocation placement within the combined heap was not instrumented.

The original 16 MiB flash was restored and verified. The private flash backup,
firmware binaries and SDKs are excluded; full executable verification requires
the CI artifact. `SHA256SUMS` covers every retained file except itself and
preserves the raw serial line endings.

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
