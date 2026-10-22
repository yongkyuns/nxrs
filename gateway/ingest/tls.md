# Authenticated gateway ingest candidate

This is the host/gateway portion of security issue #33. It implements real mutual
TLS in `nxrs-camera-ingest`, using Rustls 0.23.46, Tokio-Rustls 0.26.6, the
maintained Rustls PKI PEM API and X.509-parser 0.18.1. Dependencies remain in the
separate gateway workspace; no MCU crypto implementation is selected by this
change. The existing NuttX camera server still needs a TLS implementation and
exact entropy/socket/resource/hardware qualification. The test-only Python TLS
front end described below is not a supported device deployment or a substitute
for that work.

## Admission and lifetime policy

A secure configuration has `allow_insecure_camera_http: false` and a required
`camera_tls` object. It includes the approved site/camera/gateway IDs, issuer
certificate SHA-256, enrollment generation, and private absolute paths to the CA,
approved camera leaf, gateway-client leaf/key and CRL. All fields are required;
unknown fields, contradictory HTTP acknowledgement, public addresses and invalid
paths fail validation. `--require-camera-tls` refuses a development HTTP config.
`--check-config` validates the configuration, publisher token and all credentials
without opening a network connection or starting an encoder.

The gateway trusts only the configured, fingerprint-pinned CA. It verifies a
currently valid, directly issued P-256/SHA-256 camera certificate with the exact
`urn:nxrs:camera:SITE:CAMERA` URI and configured IPv4 SAN. The client credential
must have only client authentication usage and the exact
`urn:nxrs:ingest:SITE:GATEWAY:CAMERA` URI. Camera credentials have only server
usage; leaf CA/CRL signing is refused. The client key must match its certificate.
An actual remote peer must present the exact approved camera leaf, not merely
another same-camera certificate signed by the same CA. Rustls performs chain,
IP, validity, CRL and TLS handshake signature verification; a custom verifier
adds the exact leaf approval and delegates all cryptographic checks.

The CRL must have the approved generation, a bounded lifetime of at most one
day, known current status and a valid issuer signature. Expired, missing,
not-yet-valid or unknown revocation status fails closed. Both camera and client
revocation are checked. The root's own validity is checked too. The configured camera and client
certificates are validated before any camera connection. A client-side
authentication or handshake failure prevents the HTTP request and encoder spawn.

TLS 1.2/1.3 use the provider's secure protocol defaults; TLS 1.2 requires extended
master secret. Session resumption and early data are disabled so reconnects
reauthenticate. There is no system-root, proxy, redirect, anonymous-client,
ignore-certificate or HTTP fallback. Network/data failures retain the existing
bounded reconnect policy; authentication failure is terminal and requires
controlled recovery. The strict unit has `Restart=no`.

One source connection and one encoder remain the ownership limit. One five-second
deadline spans connect, TLS handshake, HTTP request and initial response. Existing
whole-frame/header/encoder-write deadlines, one 153,600-byte JPEG buffer and child
reaping remain in force. TLS output buffering is capped at 16 KiB; credential
inputs are bounded independently. TLS parsing and handshake memory also depend
on Rustls's internal limits; this is not an MCU memory measurement.

An active source has a maximum monotonic lease ending at the earliest CA,
camera, client or CRL expiration. The lease starts before credential I/O, not
after it. Wall time is checked during attempts at least once per second; an
observed backward jump or a forward jump past expiration closes the source and
retires the encoder. This requires a trusted gateway UTC clock; no time-free
bootstrap or certificate-time bypass is implemented. It does not establish a
trusted device clock. Expiration/termination is subject to scheduler and
existing finite encoder cleanup bounds.

Credentials are immutable during a run. Applying a new CRL, leaf or root requires
a controlled service restart, which drops the old source and reaps its encoder.
The offline `revoke` command alone does not update a live consumer or terminate
an existing connection. Provisioning/rollover automation, protected high-water
state and faster applied revocation remain tracked in #35/#37.

## Stage approved enrollment on the gateway

First follow the [offline issuer procedure](../../tools/enrollment/README.md).
Generate the gateway client key locally under its protected storage policy and
send only its CSR to the issuer. Transfer an approved public bundle to the
gateway; never copy the offline CA key there. Endpoint key generation, board RNG
quality and key storage remain separate prerequisites.

Keep the current control catalog and publisher enrollment. Do not regenerate
viewer/publisher/management tokens or reset durable denials to enable TLS. The
base ingest config must already map the intended publisher and stream path;
its publisher token path for the generated unit is
`/run/nxrs-ingest/publisher.token`. A trusted operator approves the selected
camera and client serials and checks the minimum generation against an external
current record keyed by issuer fingerprint. A signature alone does not prevent
restoration of an older signed configuration.

Using the issuer's hash-pinned CPython 3.12 environment, stage a NEW directory:

```sh
python tools/enrollment/prepare_ingest.py \
  --output /private/new-camera-tls \
  --bundle /private/approved/bundle.json --issuer-sha256 "$ISSUER_PIN" \
  --minimum-generation "$APPROVED_GENERATION" \
  --catalog /private/current/config.json --config /private/current/ingest.json \
  --camera cam_a --camera-serial "$CAMERA_SERIAL" --client-serial "$CLIENT_SERIAL" \
  --client-key /private/gateway-local/ingest-client.key
```

The tool verifies the signed bundle, current CRL, scope, serials and local key
match before writing. It never contacts the CA, generates endpoint keys,
installs trust or changes services. It preserves the existing ingest settings
and stages only the approved TLS configuration/credentials. Inputs and outputs
require owner-private, singly linked regular files in trusted directories.
The gateway client key currently uses unencrypted PKCS#8 inside that protected
boundary; file modes do not provide offline at-rest encryption. Hardware key
custody, encrypted volume/credential integration and backup protection remain
release requirements. Python/native key copies are not guaranteed zeroized.
The current key-reader boundary includes the ingest service UID, its encoder
child and the trusted administrator. Sharing a cgroup/UID does not isolate the
key from a compromised decoder; stronger component/key-provider isolation remains
a release concern in #35/#37. The Rust loader clears its raw PEM buffers on drop; that is not proof that no
provider/kernel/dump copies exist.

By default the staged JSON targets `/run/nxrs-ingest`, and the output includes a
strict `nxrs-ingest.service` with `--require-camera-tls`. It uses immutable
`LoadCredential` inputs and copies mode-0600 runtime files before startup. No new
capability, Netlink permission, writable state store or management credential is
added. Memory/task/cgroup cleanup limits are preserved. `--runtime-directory`
can select a private local test directory; a matching systemd unit is generated
only for the standard runtime directory.

An `.incomplete` output must not be used or overwritten. Inspect any partial
output and restage to a new directory. Before explicit installation, validate a
copy whose paths refer to the staged files:

```sh
nxrs-camera-ingest /private/local-check/ingest.json --require-camera-tls --check-config
```

For a local check, use `--runtime-directory /private/local-check` when preparing
that check artifact. For the unit artifact, install reviewed `ingest.json` and
`camera-{ca,peer,client}.pem`, `camera-client.key`, `camera-crl.pem` as root-owned
0600 files under `/etc/nxrs-monitor`, along with the reviewed unit. Stop the old
service group before replacing immutable credentials, preserve denial history,
and explicitly start and verify the new group using the strict-service runbook.
These are operator procedures; the staging command does not perform them.

Leaf/root rotation must test new approval, rejection of an unapproved new leaf,
retirement of old trust and successful new media. Record issuer pin, generation
and bundle hash outside the bundle under the deployment's protected epoch/state
policy. This implementation does not resist a privileged rollback of all local
configuration/state. Missing trustworthy time or generation evidence blocks a
production release, rather than enabling weaker authentication.

## Host evidence and remaining gates

`gateway/tests/tls_reference.py --ingest ...` exercises the actual Rust process
with temporary synthetic certificates and an encoder witness. It tests mutual
TLS, exact leaf/URI/issuer/generation policy, both revocation roles, client-key
mismatch, explicit HTTP refusal, bad files/TLS, TLS 1.2, incomplete-handshake
cancellation, five-second handshake recovery and interrupted-handshake retries without overlapping connections,
active CRL expiry and child retirement, approved leaf rotation and explicit root
rollover. It is not a JPEG decoding or hardware witness.

`ingest_reference.py --camera-tls` additionally runs the actual Rust camera
framing with generated JPEGs, real FFmpeg H264 publication, two WebRTC decoders,
stall/reconnect and cleanup. A TEST-ONLY Python mutual-TLS front end authenticates
the enrolled client and forwards to the unmodified Rust stream fixture over
loopback. This proves the gateway path; it does not implement board TLS or prove
server-side handshake exhaustion bounds. The hosted-only `--systemd --camera-tls`
mode tests generated credential copies and cgroup cleanup, with the existing
refusal to alter a personal host.

Required before closing #33: an actual supported board TLS provider/profile;
qualified entropy, device identity/private-key storage and UTC/bootstrap;
measured target CPU/RAM/stack/handshake/reconnect and denial-of-service behavior;
physical camera media and teardown qualification; deployed trust/revocation and
protected generation/rollback handling. The current HTTP camera profile stays a
development choice. Other UDP/network uses remain unauthenticated unless their
own secure protocol or explicitly qualified protected deployment boundary is
provided. The gateway decrypts camera footage for processing; this is not
camera-to-phone end-to-end encryption. The publisher token's existing FFmpeg
argv exposure remains open in #37.
