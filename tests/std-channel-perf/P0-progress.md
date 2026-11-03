# P0: phase sampling and image diagnosis

These changes extend the existing fixture. They are not a new queue, parker or
production transport, and no new physical timing result is supplied here.

## Seeded setup-delay profile

Keep the original `latency SAMPLES WARMUP` command as `tight-v1`. An optional
fourth argument enables `setup-counter-delay-v1` with a nonzero 32-bit seed:

```sh
# Same latency image, distinct experiment, one fresh boot per command.
python3 tests/std-channel-perf/capture.py physical \
  --build-directory target/std-channel-perf/esp32s3/latency \
  --mode latency --samples 1000 --warmup 100 --phase-seed 1 \
  --port /dev/ttyACM0 --session target/session.json \
  --output target/std-channel-perf/esp32s3/latency/phase-run-01
```

The firmware replays xorshift32, converts each word to a 1–2,000 microsecond
**nominal** setup delay and measures it against the selected counter. The seed,
profile and actual counter intervals are retained; analysis replays every warmup
and sample and rejects another seed, short/reversed/ambiguous intervals, missing
fields, unrequested pacing and an impossibly short retained observation window.
A configured frequency is still not an independent physical calibration.

Delays occur **before** the per-sample arm/blocked-state check, never inside the
send-to-receive or send-call timestamps. The original terminal receiver gate and
bounded cleanup remain. At most 1,024 samples and 128 warmups are admitted in this
profile; 2.304 seconds of maximum requested delay leaves room under the unchanged
15-second watchdog. A separate wall deadline and iteration limit bound a stalled
counter. A coarse or unvalidated counter cannot run this profile.

Pacing actively reads the counter instead of rounding every delay to the kernel
tick. This deliberately changes setup CPU/cache activity. It is a diagnostic
profile, **not** a production wait, offered-load model, idle/power measurement or
proof of uniformly sampled IRQ phase. Keep tight and paced distributions separate.
Actual phase relative to IRQs still needs a diagnostic trace. Do not infer
sleep/wake power or transport CPU utilization from the paced run's elapsed time.

The result also records a monotonic retained-window duration and 64 paired
counter-read observations (minimum/maximum ticks). These include possible
preemption; no estimated overhead is subtracted from channel observations.
Analysis reports the indices of maximum send and receive intervals so that a
terminal lifecycle outlier cannot disappear inside a summary percentile.
Historical captures remain readable without invented pacing or duration fields.

## Calibration boundary corrections

The independent-reference window must exclude a half-range CPU counter ambiguity
using an exact rational comparison, not only the modulo endpoint difference.
Frequency disagreement is compared before integer rounding; a discrepancy just
above the allowed tolerance cannot be rounded into a pass. Template/unknown
reference identities are rejected. These are validator improvements, **not** a
new physical calibration. Reference independence and common-event capture remain
operator-supplied evidence with the documented limitations.

## ROM digest warning: offline byte attribution

[The inspection archive](results/2026-11-03-p0-inspection/README.md) reproduces the
printed calculated and expected values for all three original ESP32-S3 images.
The pinned esptool builder's `--ram-only-header` mode writes visible RAM segments
and their checksum, followed by padding and flash-mapped segments; it omits the
appended SHA-256 digest. The ROM's expected bytes match that first hidden padding
header/payload, not a stored digest. This explains the specific printed values;
it is not proof of flash integrity, secure boot or a newly clean boot.

The new `inspect_esp_image.py` is read-only. It verifies the build-image hash,
visible checksum, hidden segment boundaries/alignment and exact ROM values. It
rejects changed/truncated images, mismatched or multiple logs and unsupported
layouts. **It does not clear the existing boot-warning timing blocker.** Do not
append a digest or remove `--ram-only-header` blindly: doing so also changes the
layout consumed by the simple boot code and needs its own target qualification.

## Remaining P0 acceptance

Physical reruns must use the corrected firmware and retain independent calibration,
actual allocation/stack placement and clean-boot evidence. The new phase profile
needs physical captures before drawing conclusions about the old tails. The
forensic diagnosis above is complete for the archived images; a boot-image change
and clean-board rerun are not. Longer representative load and allocation tests
remain in the separate architecture-focused workstreams.

The host CI artifact now includes the exact checked-out superproject source and
its revision/hash for offline reproduction. It excludes toolchains and submodule
contents; their existing pinned receipts remain necessary. A PR CI checkout can
be a merge revision rather than the branch head, and the archive records that
actual checkout rather than relabeling it.
