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
generated, embedded in source or included in the firmware. Update images are
unconfirmed so MCUboot can revert them. The standalone `tools/mcuboot_image.py`
also supports `--confirm` for an initial factory image.
Use separate signing authorities for incompatible products/layouts; a shared
firmware version alone does not identify the intended hardware.

MCUboot owns image authentication and boot/recovery state. Shared Rust update
delivery and startup-health policy will consume a small platform slot/boot
interface; flash operations stay in the owning HAL. Product wiring owns layout.
The packager uses upstream signing/verification and explicit geometry, independent
of the ESP32-S3 boot ROM.

This first increment does **not** enable hardware secure boot or flash encryption,
install a signature-enforcing bootloader, enforce a protected version floor, or
provide on-device update delivery/confirmation. Those and physical interrupted
update/recovery qualification remain open in [#34](https://github.com/yongkyuns/nxrs/issues/34).
No eFuse or debug/download setting is changed by these build commands.
