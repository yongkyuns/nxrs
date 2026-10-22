# Dependency review inventory

Source baseline: main `89aae7dd596c036d92e648b77477005bcbb1693a`, reviewed
2026-10-22. This inventory identifies where release dependency identities come
from. It is not an SBOM of a particular shipped image and does not imply that
all listed optional components are linked into every configuration.

| Component | Baseline identity / authoritative source | Review and release requirement |
| --- | --- | --- |
| Root Rust workspace | [Cargo.lock](../Cargo.lock), root Rust 1.90.0 toolchain | Audit the committed lock; capture target/features and actual linked dependency graph for each artifact |
| Host gateway Rust | [gateway/Cargo.lock](../gateway/Cargo.lock), separate host workspace | Audit independently; never add host dependencies to MCU workspace |
| Camera ingest TLS (host only) | [Gateway lock](../gateway/Cargo.lock): Rustls 0.23.46, Tokio-Rustls 0.26.6, PKI types 1.15.1, X.509-parser 0.18.1, ring 0.17.14 | Audit exact host graph and linked crypto; no MCU TLS or validated-module claim |
| NuttX | 13.1.0, gitlink `2b5c0de5e933281c209f954a63b99cf10c132346`; [release pin](../upstream/nuttx/release.json) | Include applicable [local patch series](../tools/apply-nuttx-patches.py) and resolved Kconfig; review Apache advisories |
| NuttX apps | Gitlink `3c772ea35b9245972e18999828f7b533e6fc57cc` | Include apps patches and selected downloaded components; gitlink alone is not the final expanded dependency tree |
| ESP32-S3 HAL/radio libraries | Pinned NuttX `arch/xtensa/src/esp32s3/Make.defs` defaults esp-hal-3rdparty to `b90b1837cb5ad24747deb4c895246037cc206ce5` | Build/cache override can change actual revision. Capture actual HAL commit, nested submodule/binary hashes and [allocator patches](../upstream/esp-hal-3rdparty/patches/); review Espressif advisories |
| Mbed TLS | Target-selected SDK submodule or NuttX-apps Kconfig download; no universal nxrs production TLS build selected | Capture actual source revision, SDK patches, crypto config, entropy hooks and link map. No validated-module or board-security claim |
| Pico CYW43439 firmware | [prepare-pico2w-radio.py](../tools/prepare-pico2w-radio.py): default Pico SDK 2.3.1 driver revision `055d64274b014dd7b1c2fc94d26e8a18face7124`, individual header/blob hashes | Record selected release, firmware/CLM hashes and license for each image; an older comparison pin remains available |
| Rust std and LLVM proposals | [LLVM pin](../upstream/rust-llvm/upstream.json), [std proposal manifest](../upstream/rust-std/proposals/series.json), build-selected patches | Capture actual selected toolchain/source/patch manifest and ABI results; proposal status is not universal release qualification |
| MediaMTX | 1.21.2, [Linux archive hash](../gateway/deploy/dependencies.json), [macOS hashes](../clients/ios/ci/dependencies.json) | Review release advisories and exact binary/config; checksum pin does not audit bundled Go dependencies |
| FFmpeg and linked codecs | Host-installed build, currently not a pinned product distribution | Record executable hash, full version/build configuration and package inventory, codec/native dependencies, current advisories and license review; this remains a release gate |
| nginx, WireGuard and host crypto | Host/distribution packages; no universal binary pin | Capture OS/package versions, TLS/crypto providers and actual generated service/firewall config; review distribution advisories |
| libWebRTC | M155, community stasel/WebRTC 155.0.0 XCFramework, SHA-256 `cb9adacf2d6bbf598d1d9e3e6c16d8fb97ca987b5b697746891828f9dc068ffe` in [Native/Package.swift](../clients/ios/Native/Package.swift) | Review upstream Chromium/WebRTC advisories, binary origin, embedded third-party notices/crypto and privacy manifest; see [THIRD_PARTY.md](../clients/ios/THIRD_PARTY.md) |
| Swift core and Apple runtime | [core package](../clients/ios/Package.swift) has no external Swift dependency; Native includes the binary above | Record Xcode/SDK/OS and actually embedded frameworks; no implication that the OS and third-party SDK share a validation boundary |
| Python integration tests | [requirements.txt](../gateway/tests/requirements.txt), [media-requirements.txt](../gateway/tests/media-requirements.txt) | Direct pins are jsonschema 4.25.1, aiohttp 3.12.15 and aiortc 1.14.0; capture resolved transitive environment and audit test tooling independently |
| Offline device enrollment | [Hash-pinned operator environment](../tools/enrollment/requirements.txt): cryptography 50.0.2, CFFI 2.1.1, pycparser 3.11; CPython 3.12 | pip-audit 2.10.1 checks these complete pins; record native OpenSSL from the actual operator image and requalify updates. No MCU or FIPS claim |
| CI actions and security tooling | Full SHA action refs; cargo-audit 0.22.2, installed with its lockfile | Review action source/transitive actions and tool updates. Dependabot proposes Cargo/action updates; approval remains separate |

At release time, use the actual build outputs and installed deployment packages,
not this table, to generate machine-readable component inventory and provenance.
Bind artifact hash, source commit/dirty state, target/profile, compiler, resolved
configuration, local patches, downloaded blobs and native dependency identities.
Identify missing/opaque vendor information explicitly instead of inventing
versions or treating a source lockfile as proof of the linked binary contents.

Review native advisories with their upstream/distribution channels. RustSec
coverage does not extend to every native component above. Track the remaining
artifact inventory, test-dependency auditing, fuzzing/sanitizers and secret scans
in [#38](https://github.com/yongkyuns/nxrs/issues/38).
