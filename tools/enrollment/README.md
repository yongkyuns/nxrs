# Offline device identity enrollment

This operator tool implements the host issuance/ledger portion of #35. It does
not generate MCU keys, install certificates, change trust stores, enable TLS,
flash hardware or approve a production target. The current camera HTTP path
remains unencrypted. The general IoT/camera baseline has no FIPS requirement;
this tool and its cryptography dependency are not claimed as a validated module.

## Trust and identity contract

The operator first authenticates the physical device, records its manufacturing
identity and approves its camera/site/gateway mapping in the existing gateway
catalog. A CSR signature proves possession of a key; it does not prove device
ownership, manufacturing identity, qualified entropy or protected key storage.
Never automatically approve network-submitted CSRs. Qualify the endpoint's RNG
and key custody using [the entropy gates](../../docs/entropy.md) before creating
a real device key. Only the CSR crosses to the offline issuer; endpoint private
keys stay at their endpoint. No endpoint key-generation command is provided.

The issuer signs P-256/SHA-256 leaf certificates with a two-year, P-256/SHA-256
site CA. Leaves last seven days by default; `--days` accepts 1–30 and never
extends beyond the CA. Requested CSR subjects/extensions are ignored. Exactly
these roles and scopes are issued from the approved catalog:

| Role | SAN | Extended key usage |
| --- | --- | --- |
| `camera` | `urn:nxrs:camera:SITE:CAMERA` and the explicitly approved IPv4 address | Server authentication |
| `ingest` | `urn:nxrs:ingest:SITE:GATEWAY:CAMERA` | Client authentication |

Camera addresses are canonical literals in RFC1918 or loopback IPv4 ranges;
public, link-local, IPv6 and DNS names are currently unsupported. Address changes
require new issuance. Leaf CA/CRL signing and key encipherment are disabled.
The issuer rejects duplicate camera IDs, unknown/wrong-site cameras, invalid
identifiers, unsupported CSR keys/signatures and invalid proof of possession.
It rejects reuse of a public key across any issuance, role, camera or renewal,
including after expiration/revocation and reuse of the CA key. Endpoint keys must
be fresh for each renewal. Uniqueness is enforced within this issuer's ledger;
a fleet spanning issuers still needs an approved manufacturing inventory.

TLS chain validation alone does not authorize a camera. A future transport
consumer must enforce the approved issuer fingerprint, exact role/URI scope,
expected gateway/camera mapping, server IP, certificate validity and revocation.
The host TLS witness tests chain, hostname, EKU and CRL behavior. It does not
implement the firmware or gateway's eventual runtime authorization policy.

## Qualified operator environment

Use a dedicated, trusted Unix operator host with a reliable UTC clock and local
filesystem. CPython 3.12 on Linux/macOS is the qualified host scope. The CLI
rejects other interpreter families/major-minor versions or dependency versions.
Install the complete direct/transitive version and release-hash pins:

```sh
python3.12 -m venv /private/operator/enrollment-venv
/private/operator/enrollment-venv/bin/python -m pip install \
  --require-hashes --only-binary=:all: -r tools/enrollment/requirements.txt
```

The operator library is [cryptography 50.0.2](https://pypi.org/project/cryptography/50.0.2/),
with CFFI 2.1.1 and pycparser 3.11. Hashes come from the exact PyPI release JSON;
only binary installations are supported by this procedure. Maintain fresh
advisory review: CI uses pip-audit 2.10.1 and fails on findings or scan failure.
Record the actual Python, cryptography and both Python-ssl/cryptography OpenSSL
versions for the operator image. Python's TLS library and cryptography's bundled
OpenSSL can differ. Version/hash pins are not an artifact SBOM or an audit of all
native crypto implementation code. Requalify changes before adopting them.

## Create and pin an issuer

Keep the issuer directory, its encrypted key/ledger and backups offline. Use a
strong passphrase from a managed secret source; the tool requires 32–1024 bytes,
which is a length check, not an entropy measurement. Store recovery material
separately from the issuer/backup. Do not put passphrases in argv or environment.

The CLI prompts without echo on an interactive terminal. For controlled
noninteractive use, `--password-fd 3` reads from an inherited, owner-private,
singly linked regular file: append `3</offline/private/issuer-passphrase` to the
command. Pipes, sockets, shared files and standard descriptors are rejected;
there is no blocking read from a password pipe or silent terminal fallback.
The descriptor is closed after reading. Protect the source file with its own
custody policy. Python/native-library secret copies are not guaranteed zeroized;
protect memory, dumps, swap and the operator account accordingly.

```sh
/private/operator/enrollment-venv/bin/python tools/enrollment/issuer.py init \
  --directory /offline/nxrs/site-a-issuer --site site_a
```

The parent directory must already exist and be trusted. The new issuer directory
is mode 0700, with owner-only, singly linked regular files. The CA private key is
encrypted PKCS#8 using the library's `BestAvailableEncryption`; the canonical
ledger is signed by that key. The CA also has digital-signature usage for
domain-separated issuer-ledger/public-bundle signatures; these signed payloads
are distinct from certificate/CRL bytes. It is never used as an endpoint key. The command prints only its public CA certificate
SHA-256 fingerprint. Verify and retain that fingerprint in a separately
approved operator record. Every later operation requires `--issuer-sha256`;
do not derive or silently accept a replacement pin from an untrusted export.
Existing issuer directories are never overwritten or reinitialized.

## Issue and export

Transfer the endpoint-generated CSR through an authenticated enrollment channel.
The CSR and approved catalog must be regular, singly linked files owned by the
operator with no group/other permissions. The catalog uses the existing gateway
JSON `cameras` entries (`camera_id`, `site_id`, `gateway_id`); it is not taken from
the CSR. Scope approval and the broader gateway config validation remain
operator responsibilities.

```sh
/private/operator/enrollment-venv/bin/python tools/enrollment/issuer.py issue \
  --directory /offline/nxrs/site-a-issuer --issuer-sha256 "$ISSUER_PIN" \
  --csr /offline/requests/camera-a.csr --catalog /offline/approved/config.json \
  --camera cam_a --role camera --ip 192.168.1.20

/private/operator/enrollment-venv/bin/python tools/enrollment/issuer.py issue \
  --directory /offline/nxrs/site-a-issuer --issuer-sha256 "$ISSUER_PIN" \
  --csr /offline/requests/gateway-a.csr --catalog /offline/approved/config.json \
  --camera cam_a --role ingest

/private/operator/enrollment-venv/bin/python tools/enrollment/issuer.py export \
  --directory /offline/nxrs/site-a-issuer --issuer-sha256 "$ISSUER_PIN" \
  --output /offline/exports/site-a-generation-3
```

`issue` commits the certificate to the signed ledger before printing its public
serial. No certificate is emitted before durable commitment. `export` creates a
new private directory containing `ca.pem`, `revocations.pem`, `cert-SERIAL.pem`
and `bundle.json`. The signed bundle binds the CA, site, generation, issued
certificate scopes and revocation times. Exports contain no private key or
passphrase. They do contain camera/gateway inventory and require privacy
controls. Revoked and expired certificates remain in the history; their presence
in an export never grants permission to use them. Select an explicitly approved,
currently valid, nonrevoked serial and its matching endpoint key.

Each CRL refresh durably reserves a new ledger generation before signing or
publishing. Its noncritical CRL number increases even when no certificate or
denial changed, and its authority-key identifier names the CA key, as required
by [RFC 5280 sections 5.2.1–5.2.3](https://www.rfc-editor.org/rfc/rfc5280.html#section-5.2.1).
Failed exports can consume sequence numbers; they never reuse them.
The signed CRL expires within 24 hours or at CA expiration, whichever is earlier.
A consumer must reject expired/missing/invalid CRLs, validate certificate dates
with a trustworthy clock and persist an external generation high-water mark.
`verify_bundle(payload, pinned_issuer, minimum_generation, now)` checks signature,
certificate/scope consistency, CRL contents/expiry and the caller's required
minimum generation. The minimum is mandatory; it is not automatically persisted
by this offline tool. Mutations and export reject a clock older than the last ledger update;
this does not establish a trustworthy clock or detect privileged clock/state rollback.
Neither a valid signature nor a fresh re-export proves that a privileged
operator did not restore an older ledger. Protected generation/epoch storage
and its live consumer integration remain release gates.

An export with `.incomplete` present must not be used. Failed exports are not
automatically deleted or overwritten; inspect and preserve them, then export to
a new directory. The tool makes no network connection and installs no trust.
Distribution, approved bootstrap, live consumer validation and active-session
termination are part of #33/#37 and are not implemented by the issuer.

## Rotate, revoke and recover

For routine renewal, create a fresh endpoint key/CSR, approve the unchanged scope,
issue/export, and explicitly stage/apply the new credential in the consuming
services. Verify the new identity and negative controls before retiring the old
credential. Deliberately bound any overlap period. Revoke the old serial, export
again and require consumers to apply the newer generation and terminate old
sessions. CRL refresh does not itself terminate an established TLS connection.
A lost/compromised endpoint requires revocation of every affected serial plus
separate publisher/viewer/recorder, management and VPN response. Never reuse the
lost key, even after an expiry, reset or transfer.

```sh
/private/operator/enrollment-venv/bin/python tools/enrollment/issuer.py revoke \
  --directory /offline/nxrs/site-a-issuer --issuer-sha256 "$ISSUER_PIN" \
  --serial "$COMPROMISED_SERIAL"
```

For transfer/factory reset, revoke old credentials under the former owner before
changing approved site/gateway scope. Verify protected-key deletion on the
endpoint, remove old trust/enrollment/VPN access and physically approve fresh
key generation/enrollment for the new owner. Hardware deletion/reset and fleet
ownership proofs are not provided by this tool. Decommissioning also requires
recording/backup retention and key-destruction procedures (#36).

Issuer updates are single-writer, signed, size-bounded atomic snapshots. Files
and directories are fsynced. A replacement failure leaves `.issuer-next`; every
normal operation then fails closed. Recovery only commits a complete, correctly
signed next generation under the pinned CA, preserving existing identities and
all prior denials. It may append one issued certificate or add one revocation;
or reserve a CRL-publication generation without changing entries; it cannot
clear a denial or replace the key. It never discards a pending file:

```sh
/private/operator/enrollment-venv/bin/python tools/enrollment/issuer.py recover \
  --directory /offline/nxrs/site-a-issuer --issuer-sha256 "$ISSUER_PIN"
```

A truncated/invalid pending file requires controlled offline investigation and
reconstruction of the intended operation from authoritative records; automatic
recovery refuses it. Keep consumers isolated until the current ledger and
external generation records are reconciled. If replacement succeeded but the
final directory sync failed, the command reports failure; inspect the valid
current ledger/export and reconcile the outcome. Retrying issuance with the
same key is rejected if it already committed. Do not delete locks/pending files
or restore an older snapshot to make an error disappear. An incomplete initial
issuer that never issued a device must be investigated and abandoned before
explicitly creating a different issuer; no reset command is provided.

Back up the encrypted ledger, pin and public issuance/revocation audit records
under separate custody, preserving the latest generation. Test restore offline
against an external current generation/epoch. If current denial state or CA key
custody cannot be established, isolate consumers and perform approved CA rollover
and endpoint re-enrollment; a stale signed backup is not safe recovery evidence.
A dedicated operator UID, trusted ancestors and local filesystem remain required.
Descriptor checks reject final symlinks, special files and public modes, but do
not inspect ACLs, enforce filesystem I/O deadlines, prevent privileged/root
writes, stop an owner unlinking a live lock, or supply hardware power-loss proof.

## Executed and remaining qualification

Run in the pinned environment:

```sh
python -m unittest discover -s tools/enrollment -p test_issuer.py -v
```

Tests create only temporary synthetic endpoint/CA keys. They exercise actual
signatures, role/SAN constraints, cross-camera/key reuse, signed ledger and CRL
tampering, expiry and stale generation checks, file/lock/FIFO rejection, output
privacy, injected write/replacement/sync errors and denial-preserving recovery.
Real loopback mutual TLS tests accept a valid pair and reject unknown roots,
wrong hostname/client usage, absent client credentials, revoked client leaves
and expired leaves. Linux and macOS CI do not upload generated key directories.

These are host issuance and TLS interoperability witnesses. Physical entropy,
endpoint CSR/key generation, hardware-protected storage, production transport
provider configuration, exact camera authorization, trusted bootstrap/time,
generation persistence, live revocation/session handling, power interruption,
CA rollover and manufacturing/ownership workflows remain open. No NuttX or
physical-device TLS qualification is claimed.

## Gateway-local staging

[Authenticated ingest](../../gateway/ingest/tls.md) uses `prepare_ingest.py` on
the gateway operator host with a public signed bundle and its local client key.
The offline CA private key is never an input. The helper binds selected serials
to the current catalog and stages a new required-TLS configuration without
installing it or resetting any existing application/VPN identity or denial.
