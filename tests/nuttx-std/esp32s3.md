# ESP32-S3 ordinary-main/std qualification

This profile builds the unchanged shared Cargo binary for the pinned ESP32-S3
NuttX board and executes its flash image in the pinned Espressif QEMU. It is
separate from the legacy library-entry build and the existing core-only fixture.
There is no app launcher, thread wrapper, alternative Rust main or async rewrite.

## Build and run

Use the host packages in `.github/workflows/nuttx-esp32s3-std.yml`, then:

```sh
bash tools/install-qemu-tools.sh
NUTTX_STD_COMPAT_FIXES=1 bash tests/nuttx-std/build.sh esp32s3
source target/qemu-tools/environment.sh
python3 tests/nuttx-std/run.py \
  --image target/nuttx-esp32s3-std/nuttx/nuttx \
  --esp32-image target/nuttx-esp32s3-std/nuttx/nuttx.merged.bin \
  --output target/nuttx-esp32s3-std
```

The existing installer checks pinned archive hashes for Rust 1.90.0.0,
GCC 14.2.0_20241119, and QEMU 9.0.0_20240606. Upstream Cargo 1.90.0 is separate
from the Xtensa compiler. No compiler or submodule pin is changed. The generated
target retains that compiler's ESP32-S3 processor, data layout and atomics, while
selecting NuttX/Unix, executable output, pthread-key TLS and GNU ld. Both original
and adapted JSON targets are retained. This is a custom target, not a claim that
the compiler already supplies a supported Xtensa NuttX target.

The ESP32-S3 profile uses the same three scoped NuttX std fixes as the other
qualified NuttX std profiles: parker mutex initialization, the fcntl-based
standard-fd startup fallback, and the NuttX-specific SIG_IGN startup correction. The Xtensa SDK's source hashes
are checked separately. Its installer uses a rust-src symlink; the private SDK
materializes that library before patching and proves the installed source did
not change. Cargo's actual std source path is checked. This remains patched-SDK
qualification, not stock Rust support or approval for production use.

The partially linked binary retains rustc's generated C main/std initialization;
only NuttX-supplied c/m/pthread library resolution is deferred. Its generated main
symbol is renamed solely for NuttX builtin registration. GNU ld, rather than the
upstream LLD used for ARM/RV32, handles Xtensa object files. The final image must
resolve real NuttX pthread/clock/startup symbols. Rust `poll` is allowed only
when its poll-specific observations among the 77 match the exact Cargo libc
artifact; `ppoll` remains rejected because its signal ABI is unqualified.
Native C poll uses the native layout.

## Required evidence and limits

The 77 C/Rust ABI witnesses use the exact target and libc artifact consumed by
rebuilt std. This pinned Xtensa SDK uses libc 0.2.174, rather than the other
profile's 0.2.175. No layout observation or compatible status is copied between
targets. The result is 76 compatible observations plus the known raw `SIG_IGN`
mismatch (`0` native, `1` Rust), contained only in the existing std startup
correction. Raw `SIG_IGN` and general signal APIs remain unqualified; descriptor
sanitization retains the `fcntl` fallback.

The same oracle requires three positive fresh kernels and the deliberate-failure
case, with no retries or longer deadlines. Normal main/arguments, all channel and
TLS-destructor assertions, CPU-peer progress, and clean NSH return remain required.
The pinned ESP32-S3 emulator requires its two hardware cores to be instantiated
for ROM loading; the NuttX profile disables SMP and runs the app on one core.
Snapshot flash preserves the hashed input. Radio and external RAM are disabled.
Compiling an image alone does not qualify execution.

## Verified result

Historical result: GitHub Actions run `36324184864` passed the full ESP32-S3
qualification at `3b4df480639ccaccc4c7c05a9732b754d91f430b`: normal Cargo binary
build, final NuttX link, all 47 observations from the earlier gate, three
positive fresh boots and one deliberate-failure boot. Each positive boot reached handwritten `main()`,
joined eight workers, delivered 2,048 ordered channel messages, exercised
bounded-queue rejection, timeout/disconnect, eight TLS destructors and the
CPU-peer progress check, then returned cleanly to NSH with status 0. The
deliberate `exit(7)` case produced no success report and NSH reported failure.

The emulator instantiates both ESP32-S3 hardware cores because its ROM setup
requires them, while NuttX SMP remains disabled. This therefore qualifies the
tested single-core NuttX execution model under emulation, not dual-core NuttX.

This still does not qualify a physical ESP32-S3 board, camera/Wi-Fi/PSRAM,
production HALs, latency bounds, or stack high-water behavior. The separate
large-heap resource probe is not run on this MCU profile. Pico and browser
qualification retain their separate boundaries.
