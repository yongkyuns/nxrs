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
