# Ordinary Rust main and std on NuttX: M1 qualification

## Full ESP32-S3 application integration

At `7e0c26a8a19c2ace54de2c172a5c5691965af754`, trusted self-hosted run
**36375036817** passed the first full ESP32-S3 camera/storage/telemetry integration
through an ordinary Rust `main()` and the patched NuttX `std` runtime.

Unlike the earlier lifecycle probe, this image links the actual NuttX camera and
storage providers, the production NuttX `std::net::UdpSocket` packet provider,
the portable services and `CameraProduct` behavior. It passed the 47-observation
baseline ABI gate and the same 42-observation socket ABI gate measured directly
against the Xtensa target's pinned libc 0.2.174. Final image inspection rejects
the former C UDP bridge.

QEMU then reached `RC_RUST_STD_MAIN BEGIN`, passed the C/Rust ABI callback,
single-core preemption control, camera/storage fault suite, three exact records
and four exact UDP summaries, printed `RC_RUST_STD_MAIN PASS`, and returned
cleanly to NSH. The injected failure command was rejected. Artifact
**10950034289** has SHA-256
`886f1abf27994de1c7fe1968de9fe4ecbc07f6aad0b31458b3022738a39a1db6`.

This qualifies full behavior under ESP32-S3 emulation, not physical camera/radio
hardware or the final production `app/nxrs` firmware packaging. The x86
NuttX simulator remains a separate core-only compatibility oracle.


This target-integration probe builds the same ordinary Cargo binary as the
browser profile, without target-specific changes to its handwritten main or
thread/channel code. Production apps, services, HALs, the root toolchain, and
external gitlinks are unchanged. This is not the complete M1 migration.

## Current exact-head result

At `3b4df480639ccaccc4c7c05a9732b754d91f430b`, the ordinary handwritten
`main()` plus Rust `std` threading/channel path passed all currently automated
qualification profiles:

- ESP32-S3 Xtensa QEMU: build, final NuttX link, target ABI gate, three positive
  fresh boots and one deliberate-failure boot.
- RV32 NuttX/QEMU: the shared thread/lifecycle probe plus the separate
  resource/descriptor recovery image.
- Pico 2 Cortex-M33: build/link/ABI qualification for the same ordinary binary;
  physical execution is still pending.
- Native Linux plus actual Chrome and Safari: the same shared Rust probe, with
  the separately documented Emscripten pthread/TLS SDK fix.

The shared probe source is unchanged across these targets. These are explicitly
patched-SDK results, not stock Rust support. Physical MCU execution, production
HAL I/O, full libc compatibility, timing bounds and complete M1 migration remain
separate gates.

## Extended result

[Run 36287691876][extended] passed at
`72b9e9651486cda3e5a01f7f6de7bd0821e718f7`. All three positive fresh boots and
one deliberate failure passed, including eight TLS destructor calls, the exact
destructor bitmap, and 64 non-yielding CPU-peer steps per positive case. The
historical pre-boot ABI gate recorded 38 compatible observations and five
explicitly unsupported pollfd observations. No general poll support was
established at that revision.

Retained artifact `10921710671` was independently checked against all four
transcripts, raw ABI tables, configuration/source proof and kernel hash:
`8f86470cdf964826331f91fc52f77c4ba07f414a4e0d3ace8cbb68f847bdf54a`.
The extended probe source SHA-256 is
`fe79b69c06e8aaa421bc3e597b16957394a2f39de625bdbf629dc25620d71a61`;
the same source also passed native and patched-SDK Chrome/Safari qualification.
At that historical revision, two NuttX SDK patches were in use. A third
startup-signal correction was added later after ESP32-S3 exposed the raw
`SIG_IGN` mismatch described below. This is a scoped runtime pass,
not a physical-board, every-libc-API or complete M1 qualification.

## First scoped result (historical)

[Run 36285527098][qualified] passed at
`4c3f88744fe32b9063f7d64adb1ab7a69869c5aa` with both NuttX SDK fixes enabled.
Three positive fresh-kernel runs produced the exact report, returned to NSH,
and reported status 0. The deliberate failure returned status 1 without a
success report. Transcripts and source/image hashes were independently checked;
a success print followed by a fault was not accepted.

Artifact `nuttx-std-qualification`, ID `10920089183`, retains the image, inputs,
SDK patches, link records, and console logs. Kernel SHA-256:
`987df162e8431bf1ad2daaadb4224d38113b2da44d9ad924b777a3f99e21f3a7`.
That historical probe's SHA-256:
`f5f0ef60e481aee587755919c7af2dfcc2a59b36d6f5d47fbc219460574f028b`.
It verified main/arguments, eight joins, distinct live thread IDs, 2,048 FIFO
messages, eight full-queue rejections, TLS values, timeout/disconnect and clean
return. It did not yet test TLS destructors or CPU-bound peer preemption.

## Current extended checks

The shared probe now also requires eight distinct worker TLS destructors and
64 CPU-peer handshake steps. Each joined pair must have the exact destructor
count and tag bitmap, preventing missed or duplicate cleanup. Paired workers
exchange atomic progress without sleep, yield, I/O, clock reads or blocking
calls inside the handshake. On the required single QEMU CPU, completion needs
preemption; native/browser parallel execution alone does not prove that property.
The build asserts `CONFIG_RR_INTERVAL=10`. This is a progress test, not a bound
on preemption latency, priority inversion, fairness or hardware deadlines.

All previous channel checks and runtime deadlines remain. Both browser and
NuttX oracles require `tls_drops=8` and `cpu_peer_steps=64`, with negative controls
for incomplete values. Every NuttX case uses a fresh kernel; process-runtime
reentry in a flat address space is not qualified. NSH reduces child exit status
to success/failure: `std::process::exit(7)` must produce status 1, not an invented
exact exit-7 result. Positive cases require status 0 and clean return.

These additions require their own exact-head CI results; the historical run
above cannot establish their success. See [PR #5][pr] for reviewed run/artifact
identities. Browser lifecycle coverage and its separate SDK fix are described
in [browser-threads.md](browser-threads.md).

### ABI gate before execution

`check-abi.py` compares native C observations from the configured NuttX headers
with Rust observations from the exact `libc`, `core`, and `compiler_builtins`
artifacts reported by that target's Cargo std build. The current gate records
77 observations, including the full 24-byte pollfd size/alignment, field offsets
and widths (`fd`, `events`, `revents`, `arg`, `cb`, `priv`), `u32` event masks,
`pollevent_t` and `pollcb_t`, and poll constants. Public layouts/constants must
match exactly; opaque pthread storage must meet native size/alignment capacity.
The result is 76 compatible observations plus one known unsupported raw
`SIG_IGN` mismatch.

Safe Rust `poll` is allowed only when all poll witnesses match the exact Cargo
libc artifact. SDKs without the pollfd fix do not qualify Rust poll, and Rust
poll imports are rejected. `ppoll` remains forbidden because its signal ABI is
unqualified.
NuttX `SIG_IGN` is 0 while Rust libc encodes it as 1; the existing std startup
correction contains that use, but raw `SIG_IGN` and general signal APIs remain
unqualified. The existing `fcntl(F_GETFD)` descriptor-sanitization path is
retained. `PTHREAD_MUTEX_RECURSIVE` remains unavailable and unqualified.

Generated C/Rust witnesses, objects/raw sections, `abi-report.json`, config hash
and exact libc artifact hash are retained. The earlier 47-observation gate,
including its 41-compatible plus six-contained result (five pollfd observations
and raw `SIG_IGN`), is historical. Earlier helper compilation failures never
counted as ABI or runtime passes.

## Scope and linking

The profile is `riscv32imac-unknown-nuttx-elf`, isolated
`nightly-2025-09-15` (rustc `52618eb338609df44978b0ca4451ab7941fd1c7a`),
and pinned NuttX `rv-virt:nsh` on one QEMU CPU. It is not a host std program
running beside NuttX. Sources/private SDK/output live under `target/nuttx-std`.

`link.py` partially links the normal binary into an ELF rooted at the
compiler-generated C ABI main, deferring only c/m/pthread resolution to NuttX.
The Makefile renames that symbol to `rust_std_main` for built-in registration;
it does not rewrite source, call a private mangled function, or remove std
initialization/cleanup. NuttX is the runnable image, not that partial ELF.

## Three scoped NuttX SDK fixes

**This is not unmodified-upstream std support.** `NUTTX_STD_COMPAT_FIXES=1`
explicitly selects all three fixes in a private SDK copy. Zero selects unmodified
source; there is no automatic fallback.

### Parker mutex initialization

Unmodified std reached main but stalled in its first channel round.
[Run 36281646713][parker] located main and worker inside the pthread parker's
mutex acquisition. The binding's zero-filled static initializer does not encode
the pinned kernel's ownership/mutex flags. The NuttX-only patch calls the
pinned mutex's existing init method before condition-variable initialization.

### Historical startup pollfd failure and current ABI fix

With only that fix, [run 36283070977][fault] printed a complete report, then
faulted at PC=0. Rust's [libc pollfd][libc-poll] is 8 bytes on RV32; the pinned
[NuttX structure][nuttx-poll] is 24 bytes. The actual ELF places the three-entry
array at sp+24 and saved RA at sp+92. The third native `priv = NULL` write in
[poll_setup][poll-impl] lands at `24 + 2*24 + 20 = 92`, corrupting that return
address. Saved registers were also overlapped. This was never accepted as a pass.

The original startup workaround excluded that poll optimization, retaining the
existing `fcntl(F_GETFD)`/open-devnull fallback. Descriptor sanitization was not
skipped. The committed libc pollfd patch now supplies the native layout, and
the std patch zero-initializes all fields before use; polling still requires the
exact ABI gate above. The fallback remains in place.

### Startup SIG_IGN mismatch

ESP32-S3 reached final linking and the earlier ABI gate, then failed before
handwritten `main()` in NuttX `signal()`. The pinned Rust libc binding encodes
`SIG_IGN` as 1, while the qualified NuttX configuration defines `SIG_IGN` as
the null handler and reserves 1 for `SIG_HOLD`. Debug assertions correctly
rejected the invalid handler.

The third NuttX-only std patch changes only Rust std's `reset_sigpipe` startup
path to use the measured native null ignore-handler value on NuttX. It retains
the actual `signal(SIGPIPE, ...)` call, error checking, inheritance behavior and
default-handler branch. The build additionally requires `CONFIG_SIG_DEFAULT`
to remain disabled. Raw libc `SIG_IGN` remains explicitly unqualified; general
signal APIs are not claimed safe by this workaround.

`prepare-std.py` checks the original source blobs, copies the toolchain,
patches only the copy, and verifies installed files remain unchanged. The
exact diffs/hashes and Cargo's selected std source path are retained. Twelve
SDK rejection controls protect against SDK drift, repatching, loss of the
fcntl fallback, and weakening of the signal-startup correction. No kernel or application change implements these fixes.

## Reproduce

Install the pinned compiler/rust-src, GNU RISC-V tools, QEMU and the NuttX
prerequisites listed in the workflow, then:

```sh
python3 tests/nuttx-std/run.py --self-test
python3 tests/nuttx-std/prepare-std.py --self-test
python3 tests/nuttx-std/check-abi.py --self-test
NUTTX_STD_COMPAT_FIXES=1 bash tests/nuttx-std/build.sh
python3 tests/nuttx-std/run.py --image target/nuttx-std/nuttx/nuttx
```

The earlier `NUTTX_STD_PARKER_FIX` variable is replaced by
`NUTTX_STD_COMPAT_FIXES`; use the explicit new setting.

## Remaining boundaries

The SDK fixes need review before production adoption. Recoverable heap pressure,
oversized-thread-stack rejection/recovery, and all standard-descriptor close
combinations are now exercised by the separate RV32 resource image. Broader
libc coverage, fragmentation/leak behavior, infallible OOM, total task-slot
exhaustion, TLS corner cases, process-runtime reentry, physical ESP32-S3/Pico
execution, Pico 2 W wireless integration, production HAL I/O and physical
deadlines remain separate qualifications. The extended checks above
must pass at their own source revision; no earlier result is substituted.
The x86 core-only simulator remains separate; ESP32-S3 QEMU full integration now
runs through the ordinary std-main path.

The relocatable link still produces Rust debugger symbolization warnings.
C frames/source/disassembly support the diagnosis; complete debugger support is
not claimed. A scoped runtime pass is not the entire firmware migration.

[qualified]: https://github.com/yongkyuns/nxrs/actions/runs/36285527098
[pr]: https://github.com/yongkyuns/nxrs/pull/5
[parker]: https://github.com/yongkyuns/nxrs/actions/runs/36281646713
[fault]: https://github.com/yongkyuns/nxrs/actions/runs/36283070977
[libc-poll]: https://github.com/rust-lang/libc/blob/0.2.175/src/unix/mod.rs
[nuttx-poll]: https://github.com/yongkyuns/nuttx/blob/433092e620a967780bfaabf4908b3d440bb7ce46/include/sys/poll.h
[poll-impl]: https://github.com/yongkyuns/nuttx/blob/433092e620a967780bfaabf4908b3d440bb7ce46/fs/vfs/fs_poll.c

[extended]: https://github.com/yongkyuns/nxrs/actions/runs/36287691876
