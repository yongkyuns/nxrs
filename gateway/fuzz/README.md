# Production parser fuzzing

This test-only workspace member calls the actual device `HttpSession` and gateway
`protocol::Reader`; it adds no product API, provider or firmware dependency.
The gateway lockfile also pins the fuzz dependencies. Production/default builds
continue to use Rust 1.90 and exclude this package.

Install `nightly-2026-03-01` with `rustup toolchain install nightly-2026-03-01
--profile minimal`, then run `bash gateway/fuzz/run.sh` on Linux or macOS.
The script uses LLVM/libFuzzer directly, avoiding a separate cargo-fuzz tool
installation/lockfile. It enables coverage guidance, AddressSanitizer, overflow
checks and debug assertions in the Rust parser/dependencies; a deliberately failing
synthetic allocation must produce an actual ASan diagnostic before fuzzing.
The unsafe positive-control binary is never linked into a product.

`http_request` varies fragmentation and nonblocking reads/writes, with at most
8192 wire bytes and 2048 polls. It checks immediate busy-client retirement,
deadline/disconnect cleanup, bounded output and recovery of owned storage.
`mjpeg` varies fragmentation across HTTP, multipart, JPEG-envelope and detection
metadata parsing. It accepts at most eight frames from 512 KiB and checks payload
bounds plus sequence/time monotonicity. Known-good startup controls require a
valid device request and two accepted gateway frames. Committed JPEG bytes are
header-only synthetic framing inputs, not decodable footage.

Each target has a 30-second smoke budget, five-second input timeout and 512 MiB
RSS limit. CI reuses the advisory job on relevant PRs and runs on main/schedules.
Generated corpus/crashes/logs stay in the printed private temporary directory;
CI uploads none of them. Reproduce and minimize failures, then retain a synthetic
regression with the production fix. A short pass is not exhaustive coverage or
absence of vulnerabilities. This does not instrument external FFmpeg libraries,
radio drivers or physical target code; their qualification remains in #38.
