# Security verification

Use Python 3.11+ and the repository Rust toolchain. Install the pinned auditor:

```sh
cargo +1.90.0 install cargo-audit --version 0.22.2 --locked
python3 -m unittest discover -s tests/host -p 'test_security*.py' -v
python3 tools/check-security.py policy
python3 tools/check-security.py audit --workspace root
python3 tools/check-security.py audit --workspace gateway
python3 tools/check-security.py audit --workspace fuzz
```

The security workflow runs on every PR, main push, manual dispatch and weekly
against a freshly fetched RustSec database. It scans each committed workspace
lockfile without target filtering, refuses a missing lockfile, and fails on
vulnerabilities, unsound advisories or scanner/database failures. Unmaintained
and yanked-crate warnings are reported for review; they are not automatically
classified as exploitable vulnerabilities. The command does not regenerate or
update dependencies. Run trusted code only: PR workflows have read-only tokens,
no retained checkout credentials and GitHub-hosted runners.

`advisory-exceptions.toml` is the only suppression source. Each exception needs
an exact advisory ID, workspace, owner, issue, rationale and quoted expiry date
strictly after today's UTC date and at most 90 days away. Duplicate/expired or
unscoped exceptions fail before invoking the auditor. A fresh temporary auditor
configuration prevents local/home `audit.toml` suppressions from hiding findings.
Replace the initial `exception = []` with `[[exception]]` tables when adding an
exception; do not append tables to that empty array. The policy check also rejects
mutable action refs and checkout steps that retain credentials. It checks literal workflow action steps, not shell downloads or
transitive action dependencies; review those separately. Dependabot proposes
Cargo and action updates; it does not authorize or merge them.

For an isolated tool installation, pass `--auditor /absolute/path/cargo-audit`.
`--database /absolute/path/advisory-db` selects a cache location and still fetches;
there is no production stale/offline bypass.

This first gate is not the full supply-chain program. The
[dependency review inventory](dependencies.md) identifies native/test components
and their pins or unresolved release identities. Track remaining native sanitizer
coverage and actual artifact-bound SBOM/provenance in
[issue #38](https://github.com/yongkyuns/nxrs/issues/38). A lockfile scan does not
audit NuttX, crypto/radio SDKs, FFmpeg, MediaMTX or libWebRTC. Their upstream
versions and local patches must be included in a release inventory and reviewed
against their respective advisories. Runtime codec packages are currently
host-installed; recording a version is not a pinned production distribution.

See [the threat model and evidence gates](../docs/security.md) and
[private reporting guidance](../SECURITY.md).

The separate [verified-boot host qualification](../docs/verified-boot-qualification.md)
builds checksum-pinned MCUboot/Mbed TLS and exercises actual RSA-3072 bootutil
policy/recovery. Its SDK host lockfile has an independent fresh RustSec gate and
an actual affected-upstream negative control; no native advisory or physical
boot qualification is inferred from that Rust scan.

[Offline application signing](../tools/boot/README.md) has a complete separate
operator requirements audit, encrypted target-scoped authority, bounded signed
image admission and real interrupted-process/native-verifier controls. The
filesystem issuance ledger is not a protected device anti-rollback counter.
Target update/confirmation integration and physical qualification remain open.

## Secret scanning and parser fuzzing

```sh
python3 tools/scan-secrets.py install --output /absolute/new/path/gitleaks
python3 tools/scan-secrets.py self-test --scanner /absolute/new/path/gitleaks
python3 tools/scan-secrets.py scan --scanner /absolute/new/path/gitleaks
```

Installation verifies a committed upstream archive hash and refuses to overwrite
an existing executable. Linux x86-64 and macOS arm64 are supported by the helper;
other hosts need a separately verified pinned executable. The scanner uses the
explicit reviewed config, ignores inline allow comments and user ignore files,
and scans all available committed Git history. CI fetches full history; a local
shallow clone provides incomplete historical coverage. Uncommitted files and
uninitialized submodule content are not covered by this Git-history scan.

Only rule/file/line/commit metadata reaches output. Raw scanner logs, matching
lines and secret values are never printed or uploaded, including errors. Safe
synthetic controls verify token detection, full redaction, narrow exclusions and
clean input. Exclusions cover only reviewed generic-key false positives for exact public
source-hash fields in detector/radio/benchmark provenance JSON; there is no blanket fixture/path skip.
Treat actual findings as private incidents: revoke/rotate first and handle
history cleanup separately; a redacted report does not revoke an exposed key.

See [gateway fuzzing](../gateway/fuzz/README.md) for the actual production parser
harnesses, pinned test toolchain, resource limits and sanitized corpus policy.
These jobs extend #38; native decoder/driver sanitizer coverage and release
SBOM/provenance remain open.


The security integration reconciles #41's previously separate parser/scanner
work with the later issuer/TLS and backup/boot controls. Recording segment/event
parsers join MJPEG/enrollment in the fuzz matrix. Every public parser receipt
requires an actual test-only ASan failure before its coverage-guided run; raw
logs/corpus remain private. Exact historical source-hash exclusions were checked
against the named Git source content and retain real-shaped token controls in
each covered file class. These are parser and scanner gates; native codec,
physical and release provenance work remains separately scoped.
