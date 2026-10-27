# MCUboot target build patches

The exact MCUboot/Mbed TLS archives are pinned in `security/boot-dependencies.json`;
the ESP-IDF HAL, host compilers, Python tools and these patch hashes are pinned in
`security/esp32s3-boot-dependencies.json`. `tools/boot/build_esp32s3.py` verifies a
private archive copy, applies only these patches with zero fuzz, then builds the
actual ESP32-S3 port. The simulator patch under `tests/boot/` is not applied here.

1. Archive builds must supply an explicit source version containing the pinned
   commit. The original unconditional `git describe` silently loses the identity
   when the authenticated input is an archive rather than a Git checkout.
2. The ESP32-S3 SDK configuration must preserve assertions. Removing the header's
   `NDEBUG` override and replacing CMake's Release flags keeps the SDK assertion
   paths active. Qualification preprocesses the actual image-validation compiler
   command and refuses any effective `NDEBUG` definition.

No signing, flash, eFuse, protected-counter implementation, SDK state machine or
HAL ABI is added by these patches. Fault-injection hardening remains off and is
explicit in the public report; this build is not a production-qualified boot chain.
