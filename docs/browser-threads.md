# Browser qualification of normal Rust threads

This isolated probe uses an ordinary Cargo binary with handwritten `main()` and
only Rust std. Production app/service/HAL code is unchanged. No executor,
application host, thread wrapper, xtask or runtime framework is introduced.
The same current probe source is used for native, browser and NuttX qualification;
these targets still have separate SDKs and qualification boundaries.

## Extended result with explicit SDK fix

[Run 36287691860][extended] passed at
`72b9e9651486cda3e5a01f7f6de7bd0821e718f7`: native execution, the rebuilt WASM
artifact, actual Chrome 153.0.8010.52 and Safari 26.6.1 all passed. Each browser
completed three positive starts, the exit-7 case and the missing-isolation case.
Each positive start verified eight TLS destructors and 64 CPU-peer steps in
addition to all earlier channel/lifecycle assertions. UI heartbeat ticks were
Chrome 40/40/40 and Safari 28/28/29; these are not performance comparisons.

The SDK fix below was explicitly enabled. The two browsers used one artifact;
downloaded reports, source proof, patch evidence and hashes were independently
checked. Artifacts: probe `10921390965`, Chrome `10920764577`, Safari `10920847819`.
Extended probe SHA-256:
`fe79b69c06e8aaa421bc3e597b16957394a2f39de625bdbf629dc25620d71a61`.
WASM SHA-256:
`7c7ae02a92fad4311e4d188ab67932cc7a7d0c4eaf19ab55adea3b56ff632bda`.
This is not an unmodified-SDK pass. The earlier missing-destructor failure remains
recorded below; no runtime assertion was weakened to obtain the passing result.

## Initial scoped result (historical)

[Run 36274987242][initial] passed at
`7511b1cec08b4c92524b08fc7f1f21bf4281513f` on September 26, 2026.
Native Rust 1.90.0 passed; actual Chrome 153.0.8010.52 on Linux and actual Safari
26.6.1 on macOS 15.7.9 each passed three positive starts and two negative controls.
Safari used Apple's safaridriver, not a substituted WebKit browser.

Both browsers ran one compiled artifact. Each positive run checked one main,
eight joins, distinct live thread IDs, TLS value isolation, 2,048 FIFO messages,
eight queue-full rejections, blocking channels, timeout/disconnect and checksum
394752. The UI heartbeat continued during 400 ms of Rust CPU work. Exit-7 and
missing-isolation controls rejected false success. These were functional and
responsiveness checks, not timing benchmarks.

Artifacts: `pthread-probe` 10917380931, `pthread-chrome-results` 10916533252,
and `pthread-safari-results` 10916538127. Their source-specific hashes are:

- WASM: `da7cf9e83c3796422a9474f1a041322e49f6da241b787ced7139c5ff420102df`.
- JavaScript: `f5b4382b27c35d91321e89dc55d83bbf929f2f88d4ee066b606c73c3de1ca42d`.
- Probe: `f5f0ef60e481aee587755919c7af2dfcc2a59b36d6f5d47fbc219460574f028b`.

This older probe did not test TLS destructors. Its success must not be used as
evidence that every std facility or the stronger current probe works unchanged.

## Stronger lifecycle check and SDK defect

The current shared probe additionally checks eight uniquely tagged TLS destructor
calls after worker pairs are joined, and 64 atomic peer-handshake steps without
sleep, yielding, I/O, clocks or blocking calls inside the handshake. Native
execution passed. Browser/host parallel progress does not by itself qualify
NuttX preemption; that inference requires the separate single-CPU target run.

At `21bcace735d2571ee15de2e0a933ca78158bcc67`, [run 36287319894][tls-failure]
failed in both actual browsers: after the first two joins, `TLS_DROPS` was 0
instead of 2. Both failed the first positive case; later cases were not executed.
These are retained runtime failures, not automation startup failures.

The pinned [Rust TLS selection][tls-source] routes Emscripten through a generic
WASM guard whose `enable()` does nothing and explicitly leaves TLS destructors
unrun. Thread creation and TLS value isolation can therefore work while cleanup
is missing. The probe assertions were retained rather than accepting that leak.

### Explicit private-SDK experiment

`BROWSER_STD_TLS_FIX=1` selects a small source-checked patch in a copied SDK.
Only `target_os=emscripten` with atomics is removed from the no-op guard branch
and admitted to the existing Unix pthread-key implementation. The existing
[key cleanup guard][tls-key] then supplies the destructor-list/runtime cleanup
callback. No custom app thread entry or manual destructor call is added.

The historical experiment checked original blob
`809ef5cfe9c60dd7ca99d587a9246751c1be338f`. The current `prepare-std.py` adapter
checks blob `1c137e52d931972b717c5e7c121b5dcb6940abc8`,
rejects missing/duplicate/already-patched anchors, and changes only the private
copy. Six rejection controls cover drift/repatching. The build verifies Cargo's
actual std source path and retains patch/source evidence in the artifact manifest.
Installed SDK files and the workspace compiler remain unchanged.

The default zero setting keeps upstream std for reproduction; it does not
silently fall back to another concurrency model. The workflow explicitly opts
into the experiment. The passing patched result is not an unmodified-SDK
pass or approval for production adoption. Use the exact-head results recorded
in [PR #5][pr] rather than inferring success from compilation or the initial run.

## Current build profile

- Target `wasm32-unknown-emscripten`, isolated `nightly-2026-11-26` and rust-src.
- [Emscripten 7.0.1](https://github.com/emscripten-core/emscripten/releases/tag/7.0.1);
  emsdk release commit `a7c808831a666cc903fedb906c171a718cd97341`.
- Rebuilt std/panic_abort with matching atomics/pthreads; do not mix an unrelated
  prebuilt std with differently configured Emscripten libraries.
- `PROXY_TO_PTHREAD=1` runs handwritten main on a worker. Four precreated workers
  include main; the probe needs at most three simultaneously. UI-thread blocking
  is forbidden. This is target startup support, not another app composition root.
- Fixed 64 MiB WASM memory, no growth, aborting panic and runtime assertions.
- COOP `same-origin`, COEP `require-corp`; the harness serves loopback HTTP.
  Production hosting requires a suitable secure context and compatible resources.
- The extended lifecycle experiment explicitly opts into the SDK patch above.

The recorded browser runs above used the earlier compiler and Emscripten 4.0.15.
They remain historical evidence for those exact artifacts. The current SDK pair
requires fresh Chrome and Safari execution through the same workflow and oracles;
successful compilation alone does not renew those runtime results.

The older compiler first failed on `crt1_proxy_main.o: undefined symbol: main`.
The selected compiler contains the [upstream entry-name fix][entry-fix], preserving
normal main without a custom C wrapper. That upstream fix is separate from the
experimental TLS guard patch.

## Required observations

Three fresh positive browser starts must produce exactly one complete report,
zero exit status, no runtime errors and continuing UI heartbeat. The report
includes `tls_drops=8` and `cpu_peer_steps=64` as well as every earlier channel
check. A deliberate failure must exit 7 without success; an unisolated page
must refuse startup. Runtime failures are not retried or ignored.

The Python oracle has 12 negative controls, including incomplete cleanup or
peer progress. Four transport controls preserve the separate bounded WebDriver
session-start budget; ordinary command/case deadlines remain unchanged. Two
browsers consume one hashed artifact, retaining capabilities, versions, transcripts,
source identity and SDK-patch evidence. Artifact retention is finite.

## Reproduce

With the pinned SDK active and compiler/rust-src installed:

```sh
cargo +1.100.0 run --locked --release -p nxrs-browser-threads
python3 tests/browser-threads/run.py --self-test
python3 tests/browser-threads/prepare-std.py --self-test
BROWSER_STD_TLS_FIX=1 bash tests/browser-threads/build.sh
python3 tests/browser-threads/run.py --browser chrome \
  --site target/browser-threads/site --expected-source "$(git rev-parse HEAD)" \
  --output target/browser-threads/chrome.json
# On macOS enable Safari remote automation: sudo safaridriver --enable
python3 tests/browser-threads/run.py --browser safari \
  --site target/browser-threads/site --expected-source "$(git rev-parse HEAD)" \
  --output target/browser-threads/safari.json
```

Chrome requires the actual browser and matching ChromeDriver. `BROWSER_BINARY`
and `CHROMEWEBDRIVER` can select their installed locations. Safari uses the real
Apple driver; neither browser is substituted with a different engine.

## Architecture and remaining boundaries

Retain ordinary main, threads and bounded channels for this candidate profile;
SDK defects should not be hidden by changing application semantics or weakening
tests. Runtime fixes need review before production use. Mobile browsers, every
std API, resource exhaustion, sustained loads, production HALs and hardware
real-time guarantees remain unqualified.

Browser HAL integration must still deliver real I/O completions to a waiting
Rust owner with bounded admission, safe ownership and working shutdown. Callbacks
must execute on a responsive browser context, not a worker trapped in a synchronous
Rust loop awaiting that callback. Keep this adaptation behind HAL/target support.
NuttX-specific results and ABI limits are in [nuttx-std.md](nuttx-std.md).

[initial]: https://github.com/yongkyuns/nxrs/actions/runs/36274987242
[tls-failure]: https://github.com/yongkyuns/nxrs/actions/runs/36287319894
[pr]: https://github.com/yongkyuns/nxrs/pull/5
[tls-source]: https://github.com/rust-lang/rust/blob/f7575a9da8e4a4fca3b5668d5a2ea7476db44b3f/library/std/src/sys/thread_local/mod.rs
[tls-key]: https://github.com/rust-lang/rust/blob/f7575a9da8e4a4fca3b5668d5a2ea7476db44b3f/library/std/src/sys/thread_local/guard/key.rs
[entry-fix]: https://github.com/rust-lang/rust/commit/f4db0a969c2a6631aefb350d7bc25ff4f900cc67

[extended]: https://github.com/yongkyuns/nxrs/actions/runs/36287691860
