# Security boundaries and release requirements

Status: security design and evidence plan for
[tracker #32](https://github.com/yongkyuns/nxrs/issues/32), reviewed 2026-10-27.
The original main baseline was `89aae7dd596c036d92e648b77477005bcbb1693a`.
The controls below describe the unmerged review stack; their executed source
cohorts and remaining gates are recorded in the
[integration evidence guide](security-integration.md). This document does not
qualify a production deployment or make those branches part of main. Activity,
detector and client changes require their own evidence before integration.

## Deployment and protected assets

The supported reference topology is an operator-controlled camera on a private
LAN, a trusted Linux gateway, and an enrolled iOS or Android client reaching HTTPS and WebRTC
through WireGuard. The gateway is authorized to decode, encode and distribute
footage. Its administrator and the phone owner are trusted in this topology.
A hosted operator outside that trust boundary requires a new design review;
the current gateway-terminated media path is not end-to-end encryption.

Protect live/recorded footage, thumbnails and identifying metadata; camera and
viewer identity; credentials and trust roots; firmware and signing keys; denial
state; recording availability; and the authenticity/freshness of displayed video.
Stable camera IDs are authorization resources, not IP addresses or VPN peer IDs.

| Boundary | Review-stack control and limitation | Required production evidence |
| --- | --- | --- |
| Sensor/DMA to camera HAL and stream | One capture owner, bounded buffers and deadlines; native drivers remain a memory-safety boundary | Exact image/driver ABI, malformed frames, overload and quiescent stop/restart; preserve DMA/callback ownership |
| Camera to gateway | Shared Rust mutual TLS, camera-bound identity/denial checks and finite handshake/frame deadlines; plaintext HTTP requires explicit development opt-in | Exact physical device entropy/time/key custody, CPU/RAM/stack/buffers and sustained reconnect/exhaustion measurements (#33/#35) |
| UDP telemetry | Shared std datagram sender; socket connect is not peer authentication | Classify payloads and protect sensitive traffic using an established secure protocol or qualified network boundary (#33) |
| Gateway encoder and media | Publisher receives its token through an inherited pipe; encoder has no publisher secret in argv/environment/input. Rust joins children before replacement; minimized encoder isolation has executed host witnesses | Native advisory closure, whole-release provenance and intended-host isolation/resource qualification (#37/#38) |
| Phone to gateway control/WHEP | HTTPS origin/trust checks, scoped expiring grants and strict durable denial with active-session termination; VPN identity remains separate | Intended phone/network transitions, operator credential custody, deployment firewall and sustained fail-stop/recovery qualification (#37) |
| WebRTC media | MediaMTX/libWebRTC terminate media encryption at gateway and phone | Verify negotiated media transport, pinned SDKs, ICE reachability and active revocation on the intended deployment; HTTPS signaling alone is insufficient |
| Local management/auth | Separate loopback listeners and management credential; local host is trusted | No management proxy/public route; unprivileged services, narrow writable state, fail-stop supervision; compromised same-host users remain in the threat analysis |
| Gateway recordings/backups | Declared LUKS2 mount, nested-plaintext refusal, camera-scoped encrypted capture, authenticated backup/decoded restore and interrupted recovery have hosted composition witnesses | Actual unattended unlock/recovery-key custody, physical power loss, privileged rollback policy and intended-host asset coverage (#36) |
| iPhone enrollment/captures | WhenUnlockedThisDeviceOnly Keychain, no credential sync; local capture files use complete data protection and backup exclusion | Real-device lock/unlock, reboot, interrupted writes, export/temporary-file review and decommissioning; simulator tests do not prove hardware data protection |
| Android enrollment/captures | Keystore AES-GCM with a nonexportable unlocked-device key, authenticated credential storage, backup/device-transfer exclusion and bounded private capture ownership | Physical lock/reboot/key-loss, backup/transfer, sharing, vendor-codec and interrupted-finalization qualification; emulator results remain scoped (#36/#37) |
| Firmware/update/manufacturing | Target-bound signed-image validators, SDK/mock-staging witnesses and NuttX build/ABI fixtures | Verified mutable boot stages, protected security-version storage, activation/confirmation, physical recovery and protected signing/provisioning keys (#34/#35) |
| Build/distribution | Pinned dependencies/actions, parser/sanitizer controls, signed source/distribution authentication and artifact-bound native inventories; the independently audited native cohort retains 43 findings | Resolve applicable advisories and bootstrap/kernel/plugin gaps, authenticate actual release artifacts and qualify signing-key lifecycle (#38) |

Relevant implementations: [ingest](../gateway/ingest/README.md),
[strict services](../gateway/deploy/strict-services.md),
[durable denial](../gateway/deploy/revocation.md),
[gateway policy](../gateway/control/src/policy.rs),
[phone trust policy](../clients/ios/Sources/NxrsMonitorCore/EndpointPolicy.swift),
[Keychain enrollment](../clients/ios/NxrsMonitor/EnrollmentStore.swift), and
[local captures](../clients/ios/Sources/NxrsMonitorCore/CaptureLibrary.swift).
The [native client architecture at the review source](https://github.com/yongkyuns/nxrs/blob/b734a974a8fbfcbe9e91bf6bae96e84bf3edc9ea/docs/client-camera-architecture.md)
defines shared protocol fixtures, per-camera ownership and exact decoded-image mapping.

## Attacker capabilities and response

| Attacker/event | Required response and remaining limitation |
| --- | --- |
| LAN/VPN participant can observe, inject, replay or connect to device ports | Require authenticated camera TLS and reject unknown/wrong-camera devices. Wi-Fi association and RFC1918 addressing do not establish application identity. Explicit HTTP development mode remains a plaintext risk. |
| Internet participant knows gateway address | Expose only intended VPN entry for the initial topology; require application authorization even inside VPN. Test IPv4/IPv6/firewall/route behavior rather than assuming a tunnel hides every port. |
| Lost phone, stolen bearer token or publisher credential | Revoke application and VPN identities independently; terminate active sessions; rotate enrollment and reject old credentials after restart. Playback expiry alone does not promise active stream termination. |
| Compromised camera supplies malicious JPEG/framing | Bound parsing, dimensions, memory, connection count and progress deadlines; terminate/reap the decoder before replacement; maintain codec patches. Envelope validation is not safe full JPEG decoding. |
| Resource exhaustion, slow readers, TLS handshake floods, disk full | Bound admissions, frame/handshake/write deadlines, processes, queues and storage. Emit health failures without unbounded retries or accumulating latency. Authenticated users can still exhaust a link or host. |
| Stolen powered-off camera/gateway storage or backup | Protect secrets and footage at rest with separately controlled keys; verify backup restore. File modes alone do not address offline reads. |
| Compromised running gateway/root or unlocked authorized phone | These endpoints can access plaintext in the reference trust model. Volume encryption does not prevent live privileged reads. Redesign key/processing placement if excluding these actors. |
| Physical reflashing, debug access, firmware rollback | Hardware-rooted verified boot, protected keys, production debug/download policy and minimum security version. No generic claim across boards; evaluate destructive provisioning separately. |
| Denial snapshot deleted, corrupted or restored from old backup | Strict startup fails on missing/corrupt state. Current store does not prove rollback resistance against a privileged operator; avoid stale restore and define a trusted recovery/epoch design if that actor is in scope. |
| Malicious dependency/build action or compromised signing pipeline | Immutable reviewed dependencies/actions, fresh advisory checks, minimal CI privileges, artifact provenance and separately protected release signing. A checksum authenticates neither a compromised publisher nor its source build. |

Cryptography must not bypass existing bounds. Provider shutdown retains callback
contexts and buffers until quiescence; failure remains sticky where cleanup is
unproven. Keep ordinary files/sockets in shared Rust std implementations and
native SDK operations behind the owning HAL's minimal ABI boundary. Flat NuttX
builds do not provide process isolation between the Rust application and native
kernel/drivers; Rust alone cannot eliminate native-driver compromise.

## Identity, trust and key lifecycle

| Material | Custody and lifecycle requirement |
| --- | --- |
| Device identity/private key | Unique per device; enroll against the intended camera/site. Qualify target RNG and protected storage before generating keys. Define replacement, ownership transfer and revocation. Never use a fleet default. |
| Gateway TLS and trust roots | Verify hostname/identity and chain; document expiration, trust-root rotation and bootstrap before reliable wall time. No disable-verification fallback. |
| Viewer, publisher, recorder and management credentials | Separate privileges and storage; random tokens are not human passwords. Rotate through controlled restart/config changes, prove old-token rejection and preserve durable denial. Avoid argv, URLs, logs and uploaded artifacts. |
| WireGuard keys | Keep phone private key on the phone. Removing a peer does not revoke application credentials and deleting Keychain does not revoke server access. Handle both. |
| Firmware signing keys | Offline or protected signing service, audited access and recovery. Plan signing-key rollover and revoked-version rejection before manufacturing. |
| Storage/backup keys | State which operator/device can unlock, how unattended restart works and how keys are recovered. Keep recovery material separate from backups; key loss must not cause silent cleartext fallback. |

For each secret, document generation, location, readers, copies, expiry/rotation,
revocation, backup, recovery and destruction. Zeroization is defense in depth,
not a claim that native stacks, compiler moves or crash dumps hold no copies.
The current Wi-Fi passphrase explicitly does not promise zeroization. See the
[device entropy trace and failure qualification](entropy.md) for the actual
std/libc/driver paths and remaining per-target gates. TLS must
use qualified entropy; merely enabling NuttX DEV_URANDOM is insufficient because
its selectable implementations include non-cryptographic generators.

## FIPS 140-3 decision

**Current decision:** target general IoT/camera deployments without a FIPS
requirement, as confirmed by the project owner on 2026-10-22. The supported
baseline is explicitly non-FIPS and makes no validation claim. Prioritize secure
transport, firmware integrity, protected keys/storage and operational controls.
Do not add a nominal FIPS feature. Reopen this decision if a customer or
procurement requirement identifies protected data, jurisdiction and in-scope
cryptographic services; the feasibility gaps below then become release gates.

[NIST CMVP](https://csrc.nist.gov/projects/cryptographic-module-validation-program/validated-modules)
validates a particular cryptographic module and version in specified operating
environments and approved configurations. Algorithm conformance or using TLS
alone is insufficient. If a requirement arises, prefer a suitable existing
validated module and obtain integration guidance from its vendor before
considering a new module validation. [CMVP transition guidance](https://csrc.nist.gov/projects/cryptographic-module-validation-program), rechecked 2026-10-27,
places FIPS 140-2 certificates on the Historical list after September 21, 2026;
plan a new FIPS-dependent system against FIPS 140-3 and verify current status.

| Crypto service | Baseline and FIPS feasibility decision |
| --- | --- |
| MCU TLS, random generation and key storage | No exact CMVP module/version/ESP32-S3-or-RP2350/NuttX operating-environment evidence has been selected. Vendor SDK availability or host-library certification cannot fill this gap. Resolve this before committing to a FIPS device architecture. |
| Gateway HTTPS and storage encryption | Host OS crypto may offer validated modules. Select the exact distribution, module certificate/security policy, approved mode, entropy and supported build only after a requirement is known; current nginx/FFmpeg installation is not such evidence. |
| Gateway authorization hashes/tokens | Rust SHA-256 and OS getrandom provide baseline services; the Cargo crate names are not CMVP certificates. Include them in a required-mode service inventory rather than assuming only encryption is in scope. |
| VPN | [WireGuard specifies](https://www.wireguard.com/protocol/) ChaCha20-Poly1305, Curve25519 and BLAKE2s. A gateway TLS provider change does not validate this tunnel. Assess the exact required protocol/module and, if needed, select another qualified VPN deployment without changing app identity. |
| WebRTC/phone | The community libWebRTC binary and DTLS/SRTP path need their own crypto/module inventory. Apple Keychain/OS platform protections do not establish validation for every bundled third-party component. |
| Boot signatures, provisioning and backups | Include signing/verification, random generation and key wrapping where the requirement demands them. A secure-boot feature name is not a module certificate. |

Before claiming a FIPS-capable configuration, record the requirement and scope;
CMVP certificate numbers and current status; module binaries/versions/hashes;
approved services/mode; exact supported operating environment and build;
entropy assumptions; vendor integration instructions; self-test/error behavior;
and evidence that all in-scope services use that path without fallback.
Unresolved target support stays a release blocker. Formal laboratory work,
fees, certificate issuance and production manufacturing are external gates,
not outcomes of a successful nxrs host test.

## Evidence and release gates

| Gate | Required evidence | Current status / owner |
| --- | --- | --- |
| Authenticated encrypted device ingress | Negative peer/trust/identity tests; exact board image and measured resource/recovery results | Open, #33 |
| Firmware integrity/update recovery | Exact bootloader/image/signing identity; corrupt/old/wrong-target and power-interruption tests; manufacturing/recovery procedure | Open, #34 |
| Qualified entropy/device credentials | Resolved per-target generator/provider; startup/error/restart evidence; provisioning/rotation/revocation procedure | Open, #35 |
| At-rest protection | Asset/key coverage including spool/backups; recording/playback/restore and lost-key tests | Open, #36 |
| Credential lifecycle | Strict durable denial, active-session termination, rotation/restart failures and no encoder argv secrets | Open, #37 |
| Supply-chain verification | Current advisory results, native dependency inventory, parser fuzzing/sanitizers, actual release SBOM/provenance | Open, #38; initial checks are only partial coverage |
| FIPS applicability | Concrete requirement or explicit non-FIPS profile decision; validated-module mapping if required | General IoT/camera, no FIPS requirement; owner confirmed 2026-10-22, #39 |
| Integrated production deployment | Intended host cold boot, physical source, actual phone/network transitions, long-duration overload/recovery and privacy review | Open, #20/#32 |

A host test proves its exercised host path. Emulator execution does not prove
physical entropy, radio security, secure boot fuses or iPhone data protection.
Checklists and test implementations are not executed evidence. Link exact
commits, resolved configs, commands, artifact hashes, environment and results
from each issue; keep missing gates open. Perform only isolated synthetic tests
until hardware/live deployment operations are explicitly authorized.

Use [NISTIR 8259A](https://www.nist.gov/publications/iot-device-cybersecurity-capability-core-baseline)
as a device capability checklist: identity, authorized configuration, data
protection, restricted interfaces, secure updates and security-state awareness.
This organizing reference is not a certification claim.


## Device enrollment tooling

The [offline CSR issuer](../tools/enrollment/README.md) implements scoped host
issuance, encrypted operator CA custody, signed denial history, bounded
certificate/CRL exports and interrupted-write recovery. It deliberately does not
generate endpoint keys or install trust. Its host tests do not qualify MCU
entropy/storage, runtime camera authorization, bootstrap/time, protected
generation storage, live session revocation or manufacturing/ownership handling.
Those gates remain tracked by #35/#33/#37.

The ESP32-S3 qualification-only boot/build frontend is described in
[the boot tooling guide](../tools/boot/README.md#esp32-s3-build-only-integration).
Its exact-key target build and unsigned NuttX payload records do not close ROM,
protected-counter, device update/admission, physical or manufacturing gates.
