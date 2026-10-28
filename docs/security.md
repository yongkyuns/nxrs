# Security design and release requirements

This is the design baseline for [security tracker #32](https://github.com/yongkyuns/nxrs/issues/32)
and the FIPS applicability decision in [#39](https://github.com/yongkyuns/nxrs/issues/39),
reviewed 2026-10-28. It defines required properties; merging this document does
not establish that a runtime, board or deployment implements them. Implementation
and executed evidence remain tracked in #33–#38, including unmerged PRs.

## Deployment assumptions and assets

The design targets general IoT/camera use: operator-controlled embedded cameras,
a trusted Linux gateway and enrolled iOS/Android phones. The reference network
uses a private camera LAN and WireGuard for remote phone access. Network location
or VPN membership does not grant application authorization. An Internet-facing
camera, an untrusted hosted gateway or a customer compliance requirement needs
an explicit review of these assumptions.

Protect footage, audio where enabled, thumbnails, detection metadata, device and
viewer identity, private keys, credentials, trust roots, firmware/signing keys,
revocation state, recording availability and displayed-media freshness. A stable
camera ID is an authorization resource; IP addresses and VPN peers are separate
identities.

The gateway decrypts and processes camera footage and terminates the phone's
media transport. Its administrator and an unlocked authorized phone can read
plaintext. Camera TLS plus WebRTC is not camera-to-phone end-to-end encryption.
Excluding either endpoint requires a different key/processing design.

## Trust boundaries

| Boundary | Required security property and limitation |
| --- | --- |
| Sensor/DMA to camera capture | One owner, bounded buffers, validated frame dimensions/lengths and quiescent stop/restart. Native drivers and DMA remain memory-safety boundaries. |
| Camera capture and local storage | Restrict raw frames, temporary files and captures to their owners. Qualify device key custody and at-rest protection separately from transport. |
| Camera to gateway | Mutual TLS with exact enrolled camera/site/gateway identity, chain/leaf policy, revocation and finite whole-operation deadlines. Define reliable-time bootstrap and trust rotation; never disable verification on failure. |
| UDP telemetry and discovery | Classify transmitted data. Sensitive data needs an authenticated secure protocol or an explicitly qualified protected network boundary; a connected UDP socket is not authentication. |
| Gateway ingest to encoder/publisher | Validate bounded framing and isolate native parsers. Encoder credentials must be absent from argv, environment and input. Pass publisher credentials only to the intended helper, and join all children before replacement. |
| Phone to control/playback | HTTPS trust/origin checks, camera-scoped expiring grants, durable denial and active-session termination. Application revocation and VPN removal are independent actions. |
| WebRTC media | Inventory the actual DTLS/SRTP implementations and negotiated transport. HTTPS signaling alone does not prove media protection. Preserve exact source-image identity for annotations; expire stale media/detection independently. |
| Local management | Separate private listeners/credentials and least-privilege service owners. No public management proxy. Same-host users, root and writable configuration remain explicit threats. |
| Gateway recording and backup | Protect spool, segments, thumbnails, history, exports and temporary copies. Require the declared encrypted storage and authenticated backup/restore; reject plaintext fallback. Define unattended unlock and separately held recovery keys. |
| Phone enrollment and captures | Use platform-protected credential storage, private bounded capture ownership and the chosen backup/transfer policy. Qualify physical lock/reboot/key-loss, interrupted writes, sharing and decommissioning. Simulator/emulator results do not prove hardware protection. |
| Provisioning and ownership | Unique per-device credentials bound to the intended camera; qualified entropy before key generation. Define enrollment, rotation, loss, transfer, reset and decommissioning without restoring stale denial. |
| Firmware/update/manufacturing | Verify every mutable executable stage, target/layout and signed image before activation. Protect signing keys and minimum security versions; preserve allowed recovery, interrupted update and failed-self-test handling. Define debug/download policy before manufacturing. |
| Build and release | Pinned inputs/actions, current advisory checks, actual parser/native sanitizer qualification and artifact-bound SBOM/provenance. Source checksums alone do not prove a trustworthy build or deployed binary. |

Behavior, resource ownership, validation, retries, deadlines and failure policy
belong in Rust. Follow the [HAL architecture](hal-platform-architecture.md),
[NuttX device access](nuttx-device-access.md) and
[native client architecture](client-camera-architecture.md). Keep SDK/ABI glue
mechanical and local to its owning provider. Shared Rust code still requires
qualification of each selected OS/board provider. Flat NuttX builds do not
isolate application code from native kernel/drivers.

## Attacker capabilities and availability

| Attacker or failure | Required response |
| --- | --- |
| LAN/VPN participant observes, injects, replays or opens device connections | Authenticate the peer and exact application identity; reject unsupported/cleartext production configuration. Wi-Fi association and private addressing are insufficient. |
| Lost phone, camera or copied token | Revoke new and active access, rotate the relevant application/VPN identities, and preserve denial across restart and controlled recovery. |
| Compromised camera supplies malformed media | Bound bytes, dimensions, queues and connection count; maintain native codec fixes, isolate decoding and reap failed workers. An envelope check is not full safe JPEG decoding. |
| Slow peer, handshake flood, stalled capture, disk full | Enforce admission, handshake/read/write/progress deadlines and bounded memory/process/storage budgets. Stop safely and expose unavailable state; do not accumulate retries or inference work. |
| Stolen powered-off storage or backup | Encrypt sensitive assets with separately controlled keys and test restore/lost-key behavior. File permissions alone do not prevent offline reads. |
| Compromised running gateway/root or unlocked authorized phone | Treat plaintext as accessible under this trust model. At-rest encryption does not protect an already unlocked endpoint. |
| Physical reflash, debug access or old signed firmware | Require a qualified boot chain, protected keys/security version and production debug policy; assess each exact board. |
| Missing, corrupt or stale restored denial state | Fail closed on missing/corrupt state; design protected generation/rollback handling if a privileged restore is in scope. Ordinary files do not establish privileged rollback resistance. |
| Compromised dependency, CI job or release signer | Minimize privileges, authenticate inputs and artifacts, review applicable advisories and keep release-signing custody separate. Record residual bootstrap/kernel/plugin risks. |

On failure, preserve overload, rollback, sticky-failure, shutdown and callback
lifetime invariants. Bound camera copy/handoff and subscriber work so slow
consumers cannot stall capture. Failed/expired inference means unavailable,
never clear. Test real child/process/resource retirement before reacquisition.

## Key lifecycle

For each device, gateway, viewer, publisher, recorder, manager, VPN, storage,
backup and signing secret, record its generator, location, readers, copies,
expiration, rotation, revocation, recovery and destruction. Avoid fleet defaults,
credential-bearing URLs/argv, logs and uploaded artifacts. Keep recovery material
separate from encrypted data and define ownership transfer before deployment.
Document zeroization limits: Rust/compiler/native stacks and crash dumps may
retain copies. A passing randomness statistical test does not qualify entropy;
test startup, unavailable/unseeded/error states and fail-closed recovery.

## FIPS 140-3 applicability

**Decision: not required for the current deployment assumptions.** The owner
confirmed general IoT/camera use with no known FIPS requirement on 2026-10-22.
This is an explicitly non-FIPS baseline. There is no selected CMVP certificate,
validated nxrs configuration or certification claim. Secure transport, firmware
integrity, key custody and operational qualification remain required.

Reopen the decision when a concrete customer/procurement requirement identifies
the protected data and in-scope services. Do not infer a legal requirement from
the use of TLS or add a nominal FIPS Cargo feature.

[NIST's validated-module registry](https://csrc.nist.gov/projects/cryptographic-module-validation-program/validated-modules)
provides certificate details and security policies. Validation applies to the
module and its permitted configuration, not an entire product merely using a
named algorithm. Before selection, check current certificate status, exact
version, approved services/mode, supported operating environment, entropy,
self-test/error behavior and vendor integration constraints. Prefer an existing
applicable validated module. Direct integration questions to its vendor; a port
or local patch cannot be assumed covered by the original validation.

| Service | Separate feasibility assessment if FIPS becomes required |
| --- | --- |
| MCU/NuttX TLS, RNG and key storage | No exact module/version/board/OS mapping is selected. Obtain vendor evidence for the actual operating environment and entropy path; host-library validation does not establish MCU support. |
| Gateway TLS, hashes/tokens and encrypted storage | Include all in-scope crypto services, not only HTTPS. Map the exact distribution/module/build/configuration to its certificate and policy. Crate names and installed packages are not that evidence. |
| VPN | [WireGuard's protocol](https://www.wireguard.com/protocol/) uses ChaCha20-Poly1305, Curve25519 and BLAKE2s. Assess the required protocol/module separately; changing the gateway TLS provider does not validate the tunnel. |
| WebRTC and phones | Inventory bundled DTLS/SRTP/libWebRTC and platform crypto independently. Platform credential protection does not validate every third-party library. |
| Signing, provisioning and backups | Include signing/verification, random generation and key wrapping where required. Secure-boot or encryption feature names are not certificates. |

Plan any new FIPS-dependent system against current
[FIPS 140-3 guidance](https://csrc.nist.gov/Projects/cryptographic-module-validation-program/fips-140-3-standards)
and verify certificate status at selection time. Retain the requirement/scope,
certificate numbers, exact module identities, security policies, approved-mode
configuration, environment and integration evidence. Unresolved target support
blocks a FIPS claim. New validation needs vendor/laboratory work and certificate
issuance; fees, lab spending and manufacturing are external gates. No nxrs host,
emulator or physical functional test substitutes for formal validation.

## Evidence matrix and release gates

The following are required deliverables. Their implementation and execution
status is authoritative in the linked issues; this design does not waive them.
Retain exact source commits, resolved configurations, toolchains, commands,
artifact identities and results. Distinguish source review, host, emulator,
NuttX ABI, physical device and formal validation evidence.

| Gate and owner | Evidence needed to close the implementation gate |
| --- | --- |
| [Authenticated ingestion #33](https://github.com/yongkyuns/nxrs/issues/33) | Required-TLS admission and peer/trust/identity/rotation refusals; actual bounded JPEG delivery; malformed input, exhaustion, cancellation and joined reconnect; exact physical board CPU/RAM/stack/buffer measurements. |
| [Boot/update integrity #34](https://github.com/yongkyuns/nxrs/issues/34) | Every mutable boot stage, protected signing/version policy, target/layout checks, allowed recovery, corrupt/wrong-key/old-image and interruption/self-test controls; physical boot/power-loss evidence and manufacturing procedure. |
| [Entropy/provisioning #35](https://github.com/yongkyuns/nxrs/issues/35) | Resolved generator and crypto consumers; startup/error/restart refusal; unique identity and protected key/time/generation custody; rotation/transfer/reset/decommissioning evidence. |
| [Recording/backups #36](https://github.com/yongkyuns/nxrs/issues/36) | Full asset/key coverage, actual record/playback/backup/decoded restore and interrupted recovery; quotas/atomic bounded I/O, lost keys, unattended unlock, physical power loss and phone data protection. |
| [Credential lifecycle #37](https://github.com/yongkyuns/nxrs/issues/37) | Durable denial and selective active termination across restart/config changes; scoped rotation/recovery/expiry; no secrets at every encoder/recorder launch boundary; least-privilege deployment. |
| [Supply chain #38](https://github.com/yongkyuns/nxrs/issues/38) | Current root/gateway/native advisory disposition; explicit justified expiring exceptions; pinned dependency inventory, production parser/native sanitizer evidence, private reporting/secret controls and actual artifact-bound release SBOM/provenance. |
| [FIPS decision #39](https://github.com/yongkyuns/nxrs/issues/39) | This explicit non-FIPS scope/decision; reopen for a concrete requirement and complete exact module/environment mapping before any future validation claim. |
| [Integrated release #32](https://github.com/yongkyuns/nxrs/issues/32) | Reviewed/merged implementation and intended-host cold boot, physical camera and phone/network transitions, sustained overload/recovery, operator custody and privacy review. |

Use [NISTIR 8259A](https://www.nist.gov/publications/iot-device-cybersecurity-capability-core-baseline)
as a capability checklist for identity, configuration, data protection,
interfaces, updates and security-state awareness. It is not an nxrs
certification. Primary FIPS and protocol references above were checked on
2026-10-28. Hardware qualification, live deployment and irreversible provisioning
remain separate from merging this design.
