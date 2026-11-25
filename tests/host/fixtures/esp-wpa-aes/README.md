# Pinned WPA AES patch inputs

These unmodified files come from `espressif/esp-hal-3rdparty` revision
`b90b1837cb5ad24747deb4c895246037cc206ce5`, the NXRS dependency pin:

| File | Upstream path | Git blob |
| --- | --- | --- |
| `aes.h` | `components/wpa_supplicant/src/crypto/aes.h` | `79bb2f5cb0890c364b9ec4b6951d00714da104b8` |
| `crypto.h` | `components/wpa_supplicant/src/crypto/crypto.h` | `7db0c1d48498b710183c234a27599496cbaa54e0` |
| `README` | `components/wpa_supplicant/README` | `ce871f52841bb7d5af1a0bf110439843c66b1f60` |
| `COPYING` | `components/wpa_supplicant/COPYING` | `7ca30301e28bd2a3e453a9ed58117bd9acbf0e1a` |

`test_esp_mbedtls_allocators.py` verifies the header SHA-256 identities before
copying them into its disposable ESP HAL source fixture. The production source
preparer still applies the complete registered ESP HAL patch sequence, including
`0007-wpa-aes-symbol-prefix.patch`. The Git-copy control restores and checks these
headers with its other source inputs; it does not skip patches or suppress patch
errors to isolate the allocator test.

These are patch inputs, not a host implementation of WPA or evidence of device
cryptographic behavior. The header copyright notices and upstream BSD license
terms in `README` and `COPYING` are retained verbatim.
