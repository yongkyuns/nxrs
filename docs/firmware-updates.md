# Signed firmware updates

The opt-in `esp32s3-camera-stream-mcuboot` profile builds the camera/TLS/PICO
application in NuttX's MCUboot image format. Its two 2 MiB slots and 256 KiB
scratch region fit the 16 MiB camera flash. It inherits the existing camera
configuration without copying its HAL implementations or resource budgets.
The pinned Espressif port reserves swap status for 512 sectors; signing includes
that trailer budget and rejects an image that no longer fits.

Install the NuttX-pinned upstream tool with `pip install -r tools/mcuboot-requirements.txt`.
The PyPI 2.2.0 release lacks the pinned source's EC public-key verification fix. Supply
`NXRS_FIRMWARE_SIGNING_KEY`, `NXRS_FIRMWARE_VERIFY_KEY` (the expected public root),
`NXRS_FIRMWARE_VERSION` and `NXRS_FIRMWARE_SECURITY_COUNTER`, then run:

```sh
bash tests/nuttx-std/stream/build.sh --platform esp32s3-camera-stream-mcuboot \
  --out /private/tmp/camera-mcuboot
```

The resulting `nuttx/nuttx.signed.bin` is signed and independently verified
before publication. NuttX's intermediate `nuttx.bin` has no signature and is
not the release artifact. Signing keys remain operator-owned inputs; no key is
generated, embedded in source or included in the firmware. Update images contain
only the signed container, without slot padding or boot-state trailers. The
updater must bound writes before the trailer and request a trial through MCUboot
after staging completes. The standalone `tools/mcuboot_image.py` supports
`--confirm` to pad an initial factory image and initialize its confirmed trailer.
Use separate signing authorities for incompatible products/layouts; a shared
firmware version alone does not identify the intended hardware.

MCUboot compares the candidate's signed security counter with the active image's
counter before a trial swap. With a readable active counter, missing or lower
candidate counters are refused; equal counters allow an older recovery release
within the same security epoch. Factory preparation requires one protected
32-bit counter, avoiding upstream's counterless-primary migration behavior.
An unconfirmed trial may still revert. Confirmation retains the newer image and
its counter for subsequent admissions. This uses authenticated image state, not
an eFuse counter; it does not prevent replay of an entire external-flash snapshot.

MCUboot owns image authentication, swapping and recovery. `hal/boot` exposes
`state`, `stage_update`, `request_trial` and `confirm`. Shared Rust admits staging
only from a stable image, stable-to-pending trial requests, and trial-to-confirmed
transitions. Failures remain sticky for that owner, including a staging reader
unwind. The NuttX
provider holds one exclusive lease while calling synchronous upstream SDK
operations. Application control links MCUboot's flash/trailer helpers, without
another verifier or crypto library. Its NuttX flash descriptors use direct,
synchronous NOR writes so setting a trailer flag does not erase neighboring
recovery state through the default MTD translation layer. The host regression
compiles NuttX's actual direct-write function against a NOR/power-cut model;
whole-device interrupted-update qualification is still required.
The SDK erase helper uses the native partition erase ioctl, with aligned,
bounded ranges and cached-read invalidation even after failure. Writing `0xff`
through a direct descriptor is not a physical erase.
Native flash partitions follow the product
layout; filesystem auto-mounting stays disabled.

`stage_update` erases only the inactive slot and streams an exact declared length,
using two 256-byte transfer buffers. It reserves the upstream-calculated trailer,
pads the last write to the provider's program unit, and verifies each write. Short,
extra or failed input cannot complete staging or activate an image through that
owner. Staging leaves the running image stable; the caller separately requests a
trial after success. The source owns I/O deadlines, and target compatibility and
startup health remain caller policy. Confirm only after startup health passes.
The native `boot-control` fixture exercises SDK state, trial and confirmation,
exclusive acquisition and repeated-query descriptor cleanup; it is not an OTA
service or automatic startup confirmation policy. Product wiring owns layout.
The packager uses upstream signing/verification and explicit geometry, independent
of the ESP32-S3 boot ROM.

Build the matching signature-enforcing second stage with:

```sh
python3 tools/build-mcuboot.py --platform esp32s3-camera-stream-mcuboot \
  --mcuboot-repo /path/to/mcuboot --hal-repo /path/to/esp-hal-3rdparty \
  --root /path/to/public-root.pem --out /private/tmp/camera-bootloader
```

The source repositories must contain the NuttX-pinned commits declared in
`platform/nuttx/mcuboot.py`. The builder exports those exact Git objects into
a fresh output directory and builds upstream's EC P-256 verifier with TinyCrypt.
Only a public key is accepted; the signing key is unnecessary on the boot build
host. `bootloader.json` records the pins, geometry, public-root and binary hashes.
The bootloader console uses UART0; the application retains its USB console.
The bootloader enables the SDK's startup watchdog. The NuttX handoff unlocks,
disables and relocks it before kernel initialization; application-health reset
and confirmation remain separate policy. The register model checks locked and
unlocked entry, preserved bits and repeated handoff. It does not qualify physical
watchdog reset timing.

These builds do **not** enable hardware secure boot or flash encryption,
install the bootloader, advance a hardware monotonic counter, or
provide on-device update delivery or a product startup-health policy. Those and physical interrupted
update/recovery qualification remain open in [#34](https://github.com/yongkyuns/nxrs/issues/34).
No eFuse or debug/download setting is changed by these build commands.

The opt-in `esp32s3-camera-stream-protected-build` profile is **build-only
factory qualification**, not a production provisioning procedure. Its chain is
ESP32-S3 ROM → RSA-3072-signed MCUboot → EC P-256-signed application. The builder
requires separate `--rom-key` and `--rom-root` inputs, uses `espsecure` from
`esptool==4.8.1`, verifies against the expected public root and checks the padded
ROM signature fits the reserved boot region. `bootloader.signed.bin` is the ROM
artifact; `bootloader.json` describes build configuration, never device state.
Application packaging uses 32-byte trailer alignment and encrypted native OTA
partitions. Factory images require `mcuboot_image.py --confirm`.

Protected builds require both protections to exist before SDK initialization;
unprovisioned hardware halts instead of entering automatic provisioning. A pinned
SDK patch rejects eFuse programming and clears staged writes. The builder records
the patch hashes and rejects an ELF that retains the physical programmer.
Factory preparation refuses older builds without this guard. These are software
safeguards, not physical qualification or permission to flash the working camera.
Build commands never flash or provision a device. Production keys, encrypted factory flashing,
ROM rejection tests and recovery/debug policy need qualification before hardware
activation. Keep disposable CI roots off devices.

Protected builds preserve MCUboot's NOR contract: explicit sector erases,
aligned writes only to erased ciphertext, and read-back verification. They remove
the upstream sector rewrite on each encrypted metadata write. Both SDK adapters
read physically erased 16-byte AES blocks as logical `0xff`; programmed blocks
use hardware decryption. Writes verify actual decrypted bytes, including `0xff`,
so a dropped program cannot pass through erased-value normalization. NuttX keeps
its existing flash mutex and internal-stack worker context.
The NuttX application helper uses the same 32-byte trailer layout and reports
32-byte writes. Its MTD proxy block size is also 32 bytes, so writing one flag
cannot reprogram neighboring ciphertext; incompatible block sizes fail the SDK
build. This does not enable protected runtime boot control.

Runtime boot control remains unavailable in the protected profile pending joined
interrupted-update qualification. MCUboot still owns swapping and recovery; the
unencrypted qualification profile remains available.
Host flash models do not qualify torn hardware programs or physical reset timing.

Prepare those files offline from the protected boot build and a confirmed
factory application:

```sh
python3 tools/prepare-protected-factory.py \
  --platform esp32s3-camera-stream-protected-build --boot-build /path/to/boot-build \
  --application /path/to/factory.signed.bin --rom-root /path/to/rom-public.pem \
  --app-root /path/to/app-public.pem --flash-key /path/to/device-xts-key.bin \
  --output /path/to/new-factory-bundle
```

The operator supplies a 32-byte `XTS_AES_128_KEY` input that must match the
device's factory-provisioned key. The tool requires the matching guarded
write-once boot build, both public signature roots and a fresh confirmed trailer.
Espressif's SDK encrypts image bytes at their flash addresses, including padding
needed for direct execution. Unwritten trailer blocks and empty secondary/scratch
remain physically erased. The tool verifies the exact logical read-back before
publishing the complete bundle.
Only ciphertext and an offset/size/hash manifest are published. This neither
provisions keys/eFuses nor qualifies hardware protection or runtime recovery;
the encrypted-update gate remains closed.
