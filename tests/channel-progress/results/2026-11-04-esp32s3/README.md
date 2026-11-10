# P1 physical ESP32-S3 observations

All 15 cases supported by the published ESP artifact completed on fresh WROOM
boots with matching image/runtime identities and valid payloads, cleanup and
joins. The complete seven-case std and six-case equal-FIFO no-repair gates pass.
The unequal-priority selected reservation stall reproduces on this board and
still fails its no-repair gate; the publication control completes without repair.

| Family | Cases executed | Observation | No-repair exit |
| --- | ---: | --- | --- |
| Unequal-priority selected Crossbeam | 2 | `select-reserved` stalls until bounded priority recovery; `select-published` completes | Reserved: 2; published: 0 |
| Prewarmed single-item std | 7 | Two Busy, one Accepted, three Taken and one Closed; no priority repair | Complete cohort: 0 |
| Equal-FIFO Crossbeam | 6 | Slot holder commits before peer API entry or selected-wait return; no priority repair | Complete cohort: 0 |

The reserved selected case verifies three registered arms, native `WAIT_SEM` and
an independently consumed ordinary wake before the important-slot stall. At the
snapshot, owner/producer/waker are READY at priorities 180/120/160. Recovery then
delivers the checked important payload and joins the workers. Its coarse
110 ms observation window is not a calibrated stall-duration bound.

Busy retains the original/protected std value with zero accepted/transferred
values; it is not delivery. The native condition-wait and sticky-closure cases
retain their individual checks. Equal-FIFO acceptance covers only the audited
one-core, effective-FIFO-180 reservation-order contract, including its admitted
participants and bounded interference. These results do not qualify a complete
three-class owner or arbitrary-priority transport.

## Coverage and physical limits

The board identifies as ESP32-S3 QFN56 rev v0.2, N16R8, USB/MAC
`esp32s3-bench-01`: 16 MiB flash, 8 MiB octal PSRAM, one configured 240 MHz core.
Each command followed a fresh USB reset with exclusive test access. A new private
full-flash backup was verified before flashing, restored afterward and verified
again; older snapshots were not used. The backup bytes and credentials are
excluded from this archive.

**All 15 boots of the four unchanged P1 images emit the legacy ROM SHA-256 warning.** Flash-byte
verification passes, but it does not resolve that warning. The separate P0
digest-image fix was not backported or used to relabel these exact P1 builds.
Clean-image qualification remains outstanding for this executable set.

The validator deliberately records physical evidence as `operator-asserted`.
Both `--require-physical` cohort gates return 2, preserving
`physical_execution_verified: false` and `physical_progress_qualified: false`.
Operator identity and flash receipts do not become independent authentication.
General progress, production transport, timing, WCET and hard realtime claims
remain false. This campaign measures progress observations, not latency.

The artifact has four builds, not the eight builds in the full ARM/QEMU campaign.
`direct-reserved`, `direct-published`, `send-reserved` and `send-reclaimed` have no
ESP images here and were **not executed physically**. No complete six-case
original Crossbeam cohort or 19-case physical replay is claimed. The earlier
19-case ARM/QEMU evidence remains separate and unchanged.

## Provenance and replay

Firmware came from successful [P1 workflow 41750025775](https://github.com/yongkyuns/nxrs/actions/runs/41750025775),
[artifact 13285154816](https://github.com/yongkyuns/nxrs/actions/runs/41750025775/artifacts/13285154816),
18,122,966 bytes, ZIP SHA-256
`9870a61656371236c9e9a82ee98b25c7372e50d4fb2f9b9fd2679d13a004fa47`.
The CI checkout was `06c6cfeb75fa03d805916c3b37b718ded25f6494`; all 158 inputs
match tested head `dc3bf1e800e4f95dba5fa05ee42f1aec26788dbf` and the independently
selected ESP build inventory. Every build receipt and referenced artifact was
validated, including pinned crate reconstruction, std sources and FIFO kernel
pins. `provenance.json` keeps firmware and verifier identities separate.

`runs/` retains raw console bytes, frozen sessions, captures, build receipts,
execution-attached analyses and separate build-bound acceptances. All 15
analyses/acceptances and both cohort reports were independently reproduced.
`observations.json` preserves case outcomes; `campaign.json` records order and
missing coverage; `cohort-gates.json` retains the exact gate exits. The operator
is recorded as `operator.txt`, not as another firmware build input.

Large firmware, crate archives and source ledgers remain in the hash-bound CI
artifact. To replay this slim archive, download and verify that ZIP, extract it
under `target/p1-esp-artifact/`, and copy this archive's `runs/` to a **new**
`target/p1-physical-replay/` directory. Restore each receipt's dependencies:

```python
from pathlib import Path
import hashlib, json, shutil
artifact = Path("target/p1-esp-artifact/esp32s3")
for run in Path("target/p1-physical-replay").glob("*/*/run"):
    receipt = json.loads((run / "build.json").read_text())
    for name, digest in receipt["artifacts_sha256"].items():
        source = artifact / receipt["mode"] / name
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
        destination = run / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
```

Use the pinned verifier revision above and Python 3.11+:

```sh
python3 tests/channel-progress/std_cohort.py target/p1-physical-replay/std --require-no-repair
python3 tests/channel-progress/fifo_cohort.py target/p1-physical-replay/fifo --require-no-repair
```

Both commands must return 0. Adding `--require-physical` must return 2. For each
selected case, run `analyze.py` with its console, `--build-receipt` and
`--require-no-repair`: reserved must return 2, published 0. Do not run the
ARM-specific complete `replay.py` against this incomplete ESP matrix.
`SHA256SUMS` covers retained files except itself, preserving original line endings.

Local checks: all 217 Python tests pass without skips using the artifact's
authenticated crate archives and `TMPDIR=/private/tmp`; the default macOS
temporary-path alias initially failed source-receipt comparisons. No fixture
code was changed. The 53-package architecture audit, 23 architecture, 10
HAL-boundary and three documentation regressions pass. The inherited separate
Security workflow failure is not resolved by this physical campaign.

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
