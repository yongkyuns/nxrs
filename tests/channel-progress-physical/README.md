# Fixed P1 ESP32-S3 campaign

This directory prepares and replays the next physical campaign for
[#255](https://github.com/yongkyuns/nxrs/pull/255), P1 of
[#252](https://github.com/yongkyuns/nxrs/pull/252). It freezes **eight existing
firmware builds and 19 fresh-boot cases**. It contains no device discovery,
flashing, reset or hardware runner. A board-connected operator must first obtain
the serialized P0/P1/P2 handoff and own backup, capture and restoration.

**There are no physical captures for these candidates yet.** The
[historical 15-case campaign](../channel-progress/results/2026-11-04-esp32s3/README.md)
remains valid under its original analyzers. Its four missing original Crossbeam
cases and retained ROM warnings are not filled by this plan or by new builds.

## Frozen inputs

[`candidate-manifest.json`](candidate-manifest.json) records the exact receipt
byte hashes, runtime build IDs, selected image hashes and original image hashes.
Changing an entry is rejected by the verifier's pinned manifest identity.

| Item | Frozen identity |
| --- | --- |
| GitHub artifact | [13323366472](https://github.com/yongkyuns/nxrs/actions/runs/41827567717/artifacts/13323366472) from run `41827567717` |
| Artifact name | `channel-progress-esp32s3-ram-prefix-digest-v1-build-only` |
| ZIP | 38,967,155 bytes; SHA-256 `590825c94af16032a6ae4c9adf6dc69597f8a585725a7d74e5c7df56de8aabf1` |
| Tested PR head | `499a4a6a18397410c1d5b12d716bd6a1b44bca56` |
| Firmware CI checkout | `9c08fb5ff5ec9237f25340b643cc19b4b2394d01` |
| Shared Git tree | `ec57055f6ca9cdd85f233ee6bc2d8560c2232ff2` |
| Firmware input inventory | 163 regular Git blobs; canonical SHA-256 `e48bcabb215f902920a7f998a49b79f2f82e3d01a06ddfa4a117bb53e7f73cef` |
| Profile | `esp32s3-service-footprint`, `xtensa-esp32s3-nuttx`, `ram-prefix-digest-v1` |
| Selected image in each build | `firmware/nuttx/nuttx.rom-digest.bin`, exactly 16 MiB |

The current host verifier has its own reported revision and dependency hashes.
These Python files live outside `tests/channel-progress/`, whose Python sources
participate in firmware identity. Updating this handoff does not relabel the
frozen images or require a new firmware build. Subsequent CI builds have their
own receipts and must not silently replace this artifact.

## Offline preflight

Use Python 3.11 or later and a checkout containing the frozen Git objects. No
SDK, serial package or board is required for these offline commands. For a
shallow checkout, fetch the two source objects without changing its branch:

```sh
git fetch --no-write-fetch-head origin 499a4a6a18397410c1d5b12d716bd6a1b44bca56
git fetch --no-write-fetch-head origin 9c08fb5ff5ec9237f25340b643cc19b4b2394d01
```

Download the linked artifact and verify its ZIP size and SHA-256 above before
extracting it. The verified archive contains 337 regular members, expanding to
455,540,814 bytes. Keep the extracted evidence read-only and preserve every
member. `BUILD_ROOT` below names its `esp32s3-ram-prefix-digest-v1` directory,
which must contain exactly the eight named build directories. `CAMPAIGN_ROOT`
is a separate, new directory; preflight refuses outputs inside the build root.

```sh
REPO=/absolute/path/to/nxrs
ARTIFACT_ZIP=/absolute/path/to/artifact.zip
BUILD_ROOT=/absolute/path/to/extracted/esp32s3-ram-prefix-digest-v1
CAMPAIGN_ROOT=/absolute/path/to/new-p1-campaign
mkdir "$CAMPAIGN_ROOT"
python3 "$REPO/tests/channel-progress-physical/campaign.py" preflight \
  --build-root "$BUILD_ROOT" --repository "$REPO" \
  --archive "$ARTIFACT_ZIP" --output "$CAMPAIGN_ROOT/preflight.json"
```

Exit `0` means all eight retained builds passed the existing image, std,
Crossbeam, ABI, configuration and paired-loader validators, the manifest pins,
and the independently derived complete Git input inventory. The selected
candidate is reconstructed byte for byte from its original and loader proof.
Exit `1` refuses invalid or missing evidence. The optional `--archive` also
checks the downloaded ZIP; without it, the report explicitly says that ZIP
verification was not supplied. Preflight does not acquire the board or establish
physical execution, boot behavior, secure boot, progress or timing.

## Fixed case order

Flash and verify each build once, then reset for every individual command.
The two suite images are reused across their cases. The expected gate column
records the prior ARM results as a reference, not new ESP observations.

| Order | Family | Case | Build | Reference no-repair exit |
| ---: | --- | --- | --- | ---: |
| 1 | Crossbeam | `direct-reserved` | `direct-reserved` | 2 |
| 2 | Crossbeam | `direct-published` | `direct-published` | 0 |
| 3 | Crossbeam | `select-reserved` | `select-reserved` | 2 |
| 4 | Crossbeam | `select-published` | `select-published` | 0 |
| 5 | Crossbeam | `send-reserved` | `send-reserved` | 2 |
| 6 | Crossbeam | `send-reclaimed` | `send-reclaimed` | 0 |
| 7 | Std | `mutex-admit-held` | `std-mutex-suite` | 0 |
| 8 | Std | `mutex-admit-released` | `std-mutex-suite` | 0 |
| 9 | Std | `mutex-take-held` | `std-mutex-suite` | 0 |
| 10 | Std | `mutex-take-released` | `std-mutex-suite` | 0 |
| 11 | Std | `condvar-wait-publish` | `std-mutex-suite` | 0 |
| 12 | Std | `condvar-publish-before-wait` | `std-mutex-suite` | 0 |
| 13 | Std | `condvar-close-wait` | `std-mutex-suite` | 0 |
| 14 | FIFO | `fifo-try-recv-reserved` | `fifo-channel-suite` | 0 |
| 15 | FIFO | `fifo-try-recv-published` | `fifo-channel-suite` | 0 |
| 16 | FIFO | `fifo-select-reserved` | `fifo-channel-suite` | 0 |
| 17 | FIFO | `fifo-select-published` | `fifo-channel-suite` | 0 |
| 18 | FIFO | `fifo-try-send-reserved` | `fifo-channel-suite` | 0 |
| 19 | FIFO | `fifo-try-send-reclaimed` | `fifo-channel-suite` | 0 |

Use one new `campaign_id` for this execution and a different
`measurement_session` for every row. The replay checks their consistency and
uniqueness. It does not infer chronological order from directory names, timestamps
or labels; `capture_order_verified` remains false. Retain the operator's actual
order and any interruption separately.

## Board-connected handoff

Before any device access, record who releases the board and who owns this
campaign, the host and explicitly selected port, board model/revision, observed
USB identity, and the intended start/end of exclusive ownership. Prior P0/P2
restoration records are not a new P1 handoff. Use one operator for the whole
sequence, with no simultaneous serial console or other flashing workstream.

The connected host must provide supervised, bounded device operations. Use an
external **300-second deadline for each backup, flash or readback command**, a
**120-second capture timeout** and a **135-second external capture deadline**.
A timeout or unexpected result stops the campaign; do not automatically retry
or replace a failed run with a successful one. These deadlines bound host
operations and make no fixture latency or realtime claim.

Before the first candidate write, create a **fresh, private full 16 MiB backup**
of the currently installed flash. Verify it against the device and record its
size, SHA-256 and readback log. An older P0/P2 backup is not a substitute. Keep
the backup bytes private and available until restoration is verified. Never
include credentials or private flash contents in the review archive.

For each new build, select exactly the `image_file` from its verified receipt,
write the complete image at offset zero without changing its bytes, and verify
the complete flashed image before capture. Retain tool version, command,
timeout/exit status, flash and readback logs. The historical operator used
`esptool.py v4.8.1`; its
[retained logs](../channel-progress/results/2026-11-04-esp32s3/backup-verify.log)
are provenance, not permission to reuse its device path, identity or backup.

Arrange restoration in the connected host runner's cleanup path **before** the
first candidate flash. After success, failure or interruption, restore the
fresh original image and verify the full flash against that backup. Record
measurement failure and restoration failure independently. Do not release the
board or report restoration complete until verification succeeds; if restoration
fails, retain ownership and surface that failure with the raw logs.

## Capture each row

Freeze a `channel-progress-session-v1` JSON before opening the port. It must
contain the fields below, populated from the current handoff and the exact build
receipt. This is a template, not a completed session:

```json
{
  "schema": "channel-progress-session-v1",
  "runtime_kind": "physical",
  "campaign_id": "UNIQUE_CAMPAIGN_ID",
  "measurement_session": "UNIQUE_CAMPAIGN_ID-CASE_MODE",
  "board": "OBSERVED_BOARD_MODEL_AND_REVISION",
  "board_serial": "OBSERVED_DEVICE_SERIAL",
  "active_cores": 1,
  "build_id": "EXACT_BUILD_ID_FROM_BUILD_JSON",
  "image_sha256": "EXACT_SELECTED_IMAGE_SHA256_FROM_BUILD_JSON",
  "flash_verification": "RETAINED_FLASH_AND_READBACK_LOG_REFERENCES"
}
```

For each manifest row, set `FAMILY`, `MODE` and `BUILD_MODE` from that row;
`PORT` is the port already selected during handoff, and `SESSION` is its frozen
session file. The capture output directory must be new:

```sh
BUILD="$BUILD_ROOT/$BUILD_MODE"
RUN="$CAMPAIGN_ROOT/runs/$FAMILY/$MODE/run"
python3 "$REPO/tests/channel-progress/capture.py" physical \
  --build-directory "$BUILD" --mode "$MODE" --port "$PORT" \
  --session "$SESSION" --output "$RUN" --timeout 120
```

Start capture before resetting. Wait for **`Serial capture ready`**, then have
the operator reset the board; do not reset on a fixed sleep. Capture must retain
the ESP ROM prefix, NuttX boot, exactly one echoed command, two fixture records
and the returned prompt. No second command runs on that boot. Preserve the raw
capture and failure output even if the command times out or validation refuses.

After a valid capture, save its separate build-bound acceptance report and the
actual exit code. The capture's `analysis.json` includes the execution session;
`acceptance.json` intentionally comes from the command below without a session:

```sh
gate=0
python3 "$REPO/tests/channel-progress/analyze.py" "$RUN/console.log" \
  --build-receipt "$RUN/build.json" --output "$RUN/acceptance.json" \
  --require-no-repair || gate=$?
test "$gate" = 0 || test "$gate" = 2
```

`0` is observed no-repair acceptance; `2` is a valid diagnostic that refuses
that gate; `1` is invalid evidence. Retain every valid outcome, including a
reservation stall and its bounded repair. If an outcome differs from the table,
retain it and stop for review of the scheduling evidence. Do not discard it,
silently rerun it or call a reference expectation an observed result.

## Complete replay and scoped gates

After all 19 cases are retained, generate the three original cohort reports.
These saved reports are required and must match independent replay exactly:

```sh
python3 "$REPO/tests/channel-progress/cohort.py" "$CAMPAIGN_ROOT/runs/crossbeam" \
  --output "$CAMPAIGN_ROOT/crossbeam-cohort.json"
python3 "$REPO/tests/channel-progress/std_cohort.py" "$CAMPAIGN_ROOT/runs/std" \
  --output "$CAMPAIGN_ROOT/std-cohort.json"
python3 "$REPO/tests/channel-progress/fifo_cohort.py" "$CAMPAIGN_ROOT/runs/fifo" \
  --output "$CAMPAIGN_ROOT/fifo-cohort.json"
python3 "$REPO/tests/channel-progress-physical/replay.py" "$CAMPAIGN_ROOT" \
  --repository "$REPO" --output "$CAMPAIGN_ROOT/replay.json" \
  --require-no-repair std fifo --require-clean-boot
```

The complete replay requires all 19 exact run paths, eight manifest-bound
receipts, matching source/configuration/profile and board declarations, one
campaign ID, 19 distinct session IDs, every retained proof and image, exact
per-case analyses and acceptance reports, and the three saved cohort reports.
It rejects symlinks, nonregular files, missing/extra cases, mutated evidence and
unsupported qualification flags. It independently binds the complete ESP source
inventory to regular blobs at the frozen Git revision; it never executes code
from the artifact.

| Requested decision | Meaning |
| --- | --- |
| No optional gate | Exit `0` for a complete valid diagnostic, even if a reservation stalls or a ROM warning remains |
| `--require-no-repair std fifo` | Exit `0` only if both named contracts pass; a valid refusal is `2` |
| `--require-no-repair crossbeam` | Refuses with `2` if any original Crossbeam case needed repair |
| `--require-clean-boot` | Requires the exact ESP32-S3 ROM banner before the NuttX banner and no digest-failure warning in any of the 19 retained captures; missing or misplaced prefixes and warnings refuse with `2` |
| `--require-physical` | Exit `2`: these records do not independently authenticate device identity, flash/readback, ownership or restoration |
| Invalid/missing/mixed evidence | Exit `1`, before any optional acceptance decision |

The complete report is retained even when an optional gate returns `2`. Output
paths must be new; subsequent replay should use another output filename or
stdout. The historical 15-case archive cannot pass this complete-candidate gate;
continue replaying it with the original tools and receipts.

Keep `preflight.json`, the frozen manifest, all raw captures and retained build
dependencies, the three cohort reports, gate exits, operator handoff/order and
backup/restoration receipts together. Produce a whole-archive file-hash manifest
after the evidence is final, then check it after transfer and rerun the complete
replay from a different directory. A file-hash manifest establishes transfer
integrity, not physical authenticity. Keep private backup bytes outside that
archive. Do not prune proof files or replace them with machine-specific links.

## Qualification boundary and host tests

`execution_evidence` remains `operator-asserted` and
`physical_execution_verified` remains false. A complete clean-boot observation
is a statement about the retained text, not secure-boot authentication or a
production firmware qualification. No-repair acceptance is limited to the named
family contracts. General priority progress, the full three-class owner,
production transport, timing and hard realtime bounds remain separate gates.

```sh
python3 -m unittest discover -s tests/channel-progress-physical -p 'test_*.py' -v
```

Host tests use synthetic evidence to exercise refusals and gate decisions. They
are not device observations. Existing P1 firmware, source hooks, capture code,
legacy analyzers and P0/P2-owned implementations remain unchanged by this host
campaign tooling.
