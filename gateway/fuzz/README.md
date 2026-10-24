# Gateway parser fuzzing

This independent test workspace calls the production gateway code. It does not
copy its parser or enter MCU production dependency graphs. Its lockfile retains
the exact gateway registry dependency identities plus the fuzz-only dependencies;
the security policy check rejects stale/mismatched identities.

`mjpeg` feeds bounded, fragmented in-memory input through the actual multipart
HTTP/JPEG-envelope reader. The first byte selects read chunk size; the remainder
is wire input. Assertions cover accepted length, sequence/clock monotonicity and
payload access. Each iteration handles at most eight frames and 512 KiB input.
Seeds include multiple frames, repeated sequence, oversize length and a maximum
153,600-byte frame. JPEG seed data tests framing and marker/dimension checks;
it is not claimed to be decodable camera footage.

`recording_csv` calls the exact segment-list parser used by the activity worker,
with the same 1024-byte line bound, filename/path constraint and finite duration
checks. `recording_event` calls the same bounded JSON/Event validation used by
live store inventory, with a 4096-byte limit. The activity crate’s `probe` feature exposes test entry points
perform no file, network or child-process operation. Normal gateway and firmware
builds do not enable these entry points. Seeds exercise valid records, path
escapes, Unicode, non-finite/oversized durations, integer limits and metadata.

`enrollment` calls actual JSON deserialization and gateway configuration
validation with at most 64 KiB. Assertions enforce accepted-listener and resource
boundaries. The synthetic enrolled camera contains no usable credentials.

```sh
rustup toolchain install nightly-2026-03-01 --profile minimal --component rust-src
cargo +nightly-2026-03-01 install cargo-fuzz --version 0.13.2 --locked
cargo +nightly-2026-03-01 fetch --locked --manifest-path gateway/fuzz/Cargo.toml
mkdir -p gateway/fuzz/corpus/mjpeg gateway/fuzz/corpus/enrollment
cargo +nightly-2026-03-01 fuzz run --fuzz-dir gateway/fuzz mjpeg \
  gateway/fuzz/corpus/mjpeg gateway/fuzz/seeds/mjpeg -- \
  -max_total_time=60 -timeout=5 -rss_limit_mb=1024 -max_len=524289
cargo +nightly-2026-03-01 fuzz run --fuzz-dir gateway/fuzz enrollment \
  gateway/fuzz/corpus/enrollment gateway/fuzz/seeds/enrollment -- \
  -max_total_time=60 -timeout=5 -rss_limit_mb=1024 -max_len=65536
```

The first corpus directory receives generated inputs; keep it separate from the
committed hand-built seeds. Generated corpora, crash artifacts and coverage are
ignored. Reproduce and minimize real failures, then retain a sanitized synthetic
case and a production regression test with the fix. Never upload private frames,
credentials or captured production inputs. CI deliberately does not upload raw
fuzz artifacts.

Cargo-fuzz enables coverage guidance, AddressSanitizer, debug assertions and
overflow checks by default. CI runs all four targets on GitHub-hosted Linux with a
pinned test compiler and a sixty-second fuzz budget per target, and checks that
the committed lockfile did not change. Production remains on Rust 1.90.0.
A short pass establishes harness execution, not absence of vulnerabilities.
This does not instrument the external FFmpeg decoder, vendor radio code or
NuttX drivers; native sanitizer qualification remains a separate #38 gate.


The integration restores the implementation from draft #41 while preserving the
later operator TLS, issuer and authentication jobs. The fuzz lock is refreshed
against the current gateway (Tokio 1.51.6); an inactive `publisher-sdk` feature
retains the exact optional native SDK dependency identities without enabling or
instrumenting those codecs in ordinary parser fuzz runs.

## Bounded qualification receipts

After the source is committed and clean, put the pinned cargo-fuzz on PATH and run:

```sh
python3 gateway/fuzz/run.py --target recording_csv --work /absolute/new/private-run
```

The runner requires the pinned compiler/tool, an unchanged committed source and
lock inventory, and finite process/output/input/time/RSS limits. It first builds
and executes `asan_probe`, a test-only misuse of a synthetic owned allocation.
The actual AddressSanitizer heap-use-after-free diagnostic is mandatory; an
uninstrumented or successful probe cannot qualify. This probe never enters a
product executable. The parser then runs up to 100,000 coverage-guided iterations
with a 60-second budget. The receipt records actual execution/coverage/RSS counts,
exact source and fuzzer/probe/tool hashes, compiler identity and scope flags.
Coverage edges describe this binary's instrumentation, not complete code coverage.

Only `qualification.json` is uploaded. Logs, generated corpus and crash inputs
remain private and are not uploaded, even for a failed run. The owned process
group is retired before reaping its reserved leader PID. The controlled crashing
probe demonstrates sanitizer activation; it is not a native codec/driver test.
No hardware, FIPS, deployment or absence-of-vulnerabilities claim follows.

The refreshed test lock updates fuzz-only `anyhow` to 1.0.104 after the advisory
gate rejected 1.0.102 for [RUSTSEC-2026-0190](https://rustsec.org/advisories/RUSTSEC-2026-0190.html).
No exception or production gateway dependency change is used.
