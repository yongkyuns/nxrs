# ESP32-S3 protected IMU ABI witness

The witness links the production IMU I²C and GPIO C bridges into a protected
NuttX userspace executable. The pinned ESP32-S3 DevKit `knsh` configuration
supplies the kernel and userspace split. Test-only lower halves register
deterministic I²C and GPIO devices with the actual NuttX VFS upper halves.

## Cross-build

1. Initialize `external/nuttx` and `external/nuttx-apps` at the Nxrs gitlinks.
2. Put GNU Make, GNU sed on macOS, the NuttX Kconfig tools (`kconfig-tweak`
   and `olddefconfig` or `kconfig-conf`), `genromfs`, Python 3, `esptool.py`
   and the ESP32-S3 Xtensa compiler and binutils on `PATH`. CI uses the
   repository's pinned Espressif GCC 14.2.0 tool bundle.
3. Run from the Nxrs checkout with a new output directory. The script stages
   the exact source revisions and creates separate kernel and userspace images.

   ```sh
   bash tests/nuttx-std/imu-protected/build.sh /tmp/nxrs-imu-protected
   ```

   For an offline build, set `NXRS_IMU_WITNESS_HAL_CACHE` to an existing Git
   checkout of Espressif's third-party HAL with its mbedTLS submodule initialized.
   The script still uses the NuttX-pinned revisions of both repositories.

The build records the effective configuration, source pins, compiler, ELF
symbol placement, disassembly of the `ioctl` and `poll` syscall stubs, and image
hashes. `nuttx/` contains `nuttx`, `nuttx_user.elf`, `nuttx.bin` and
`nuttx_user.bin`. CI cross-links the witness in the existing ESP32-S3 event-demo
job and retains the build evidence.

## Target execution

1. On an authorized ESP32-S3 DevKit, record the wiring, console settings and
   baseline. Prepare and verify the backup and recovery procedure before loading
   the image. Use the flash layout from the effective DevKit configuration.
2. Capture the boot console and require `S8_IMU_PROTECTED_ABI_PASS` within
   a declared run deadline. A `FAIL` marker, panic or missed deadline fails the
   run. The I²C path covers repeated-start messages, nested buffer pointers,
   canaries, error propagation and reopen. The GPIO path covers ioctl pointer
   writes, poll readiness, cancellation, `EINTR` and interrupt unregistration.
3. Restore the baseline and verify its behavior. Attach the sanitized console
   log with the configuration, image hashes and restoration result to S8.

Cross-linking establishes the build mode and ABI placement. A successful boot
also exercises the VFS syscall boundary with deterministic test devices. The
GPIO edge comes from a kernel timer, and I²C reads come from a test lower half.
Physical IRQ timing, sensor behavior and the Rust acquisition lifecycle require
the [S8 physical matrix](../../../docs/peripheral-composition-plan.md#s8--qualify-protected-execution-and-physical-deadlines).

## Rust runtime witness

Build the same qualification application for flat and protected CoreS3. It
starts eight acquisition workers in succession, cancels each through a bounded
notification, joins it, and checks its Rust thread-local destructor ran once.
Both profiles directly start one application. The protected build uses
group-local pthread keys. The fixture exercises worker lifecycle without a sensor.

```sh
cargo +1.90.0 nxrs build \
  --manifest-path tests/nuttx-std/imu-protected/runtime/Cargo.toml \
  --profile cores3-imu-qualification --out target/s8-runtime-flat
cargo +1.90.0 nxrs build \
  --manifest-path tests/nuttx-std/imu-protected/runtime/Cargo.toml \
  --profile cores3-protected-qualification --out target/s8-runtime-protected
```

The protected output records the kernel and userspace flash offsets, sizes and
hashes in `protected-images.json`. The pair requires a matching legacy ESP32-S3
bootloader and partition table. Use the effective configuration to verify that
layout and both image hashes before loading an authorized board. Use the CoreS3
console selected by `resolved.config`.

For each mode, require exactly one `S8_PROTECTED_RUST_PASS cycles=8 tls_destructors=8`
within 60 seconds of boot. A failure marker, panic, reboot before the pass, or
missed deadline fails the run. Restore and verify the baseline afterward.
The host run and target cross-links are recorded separately from these target
runtime results.
