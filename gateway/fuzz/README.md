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
overflow checks by default. CI runs both targets on GitHub-hosted Linux with a
pinned test compiler and a sixty-second fuzz budget per target, and checks that
the committed lockfile did not change. Production remains on Rust 1.90.0.
A short pass establishes harness execution, not absence of vulnerabilities.
This does not instrument the external FFmpeg decoder, vendor radio code or
NuttX drivers; native sanitizer qualification remains a separate #38 gate.
