# Qualified backup engine build

This is the host-only native dependency increment for #38 and #36, based on the
layout-v2 recording/backup profile. It does not change a HAL or add Go/native
libraries to MCU builds. It is an nxrs rebuild of restic 0.19.1 with reviewed
module updates and Go 1.27.3, **not** the official 0.19.1 release binary. Its native
repository format remains restic version 2. No FIPS mode or certification is
claimed.

The previous official binary embeds Go 1.26.4 and dependencies reported by the
fresh Go vulnerability database. A clean Cargo audit does not cover them. The
helper now accepts only the rebuilt executable hashes in
[backup-dependencies.json](backup-dependencies.json); the old official hashes
are refused before handing a key to the native child. Upgrade the helper,
engine and private configuration's `restic_sha256` together. Keep the existing
protected layout-v2 worker/controller cohort, separate key custody, trusted
root-owned executable/parents and stopped-group requirements from
[encrypted-backup.md](encrypted-backup.md).

## Build and verification

Use Python 3.12+ on Linux amd64 or macOS arm64. Nothing is installed and no service,
credential or device is changed:

```sh
python3 gateway/tools/backup_engine.py --artifact linux_amd64 --output /private/new-restic --qualify
python3 gateway/tools/verify_backup_evidence.py /private/new-restic --require-qualification
```

Use `darwin_arm64` on the matching Mac host. Existing output names are refused.
`--cache /private/public-download-cache` optionally retains only public archives;
every cached archive is rehashed, and a mismatched entry is refused. Compiler,
module and scanner working directories remain fresh per invocation. Environment
variables and local Go settings/workspaces are not inherited by native tools.
Downloads use HTTPS, committed archive hashes and Go's checksum database. No
floating toolchain, native self-update build tag or runtime download is selected.

The committed source tarball, official host Go SDK, replacement `go.mod`/`go.sum`,
scanner module identity and final executable hashes are independently checked.
The frozen graph is checked before and after qualification. A different output
hash, changed module files, failed tool or incomplete scan prevents admission.
A Go symbol-bearing build is retained so binary scanning need not fall back to
all symbols in every module. The actual embedded build metadata must match the
reviewed target/flags and `CGO_ENABLED=0`.

Every build runs fresh source and compiled-binary govulncheck scans. JSON mode's
zero exit code is **not** a clean audit: any affected imported package or called
symbol fails. Module-only advisories for unimported packages remain visible in
the bound report; they are not ignore-list exceptions. Currently that includes
GO-2026-5932 for deprecated OpenPGP within x/crypto, without an affected package
import. Static analysis cannot prove absence of every vulnerability or account
for every reflective/unsafe path. The extracted main module has `(devel)` Go
metadata; a separate official module-index check refuses any new upstream
restic advisory until its exact source/version is explicitly reviewed.

`--qualify` also executes upstream crypto/repository/restorer/local-backend tests
on the matching native host, rejects the checksum-identified affected binary
through the advisory gate, and checks actual legacy-format interoperability.
The affected binary is executed **only** in a fresh owned public synthetic
fixture to create a repository; the rebuilt engine checks and restores its exact
payload. No live backup, production recovery secret, remote backend or footage
is involved. The disposable Linux encrypted-volume job separately executes the
actual unprivileged helper/API, real decoded media and interruption recovery.
Cross-compilation alone is not that execution evidence.

## Bound public evidence

A successful build creates the new executable and three adjacent JSON files:

- `.cdx.json`: CycloneDX 1.6 inventory of the executable, standard-library version
  and module identities embedded in the actual compiled binary. Go h1 content
  sums are properties, not mislabeled archive SHA-256 values.
- `.advisories.json`: scanner/database identities, source/binary outcomes, visible
  unimported module advisories, upstream-index digest and qualification status.
- `.provenance.json`: an in-toto/SLSA-v1-shaped **unsigned build record** binding
  the executable, source/SDK/module/recipe/builder digests and report digests.

The verifier checks those bindings against this checkout, the pinned executable,
actual module/version/content records and requested qualification status. Tests
mutate a real built bundle, including coherent rehashing of malicious inventory
or advisory reports, to ensure simple JSON/process success cannot bypass policy.
Only public executables/reports are retained by the native qualification job;
raw tool logs, keys, caches and synthetic repositories are not uploaded.

These records do not authenticate a release publisher or establish a SLSA level.
Trusted release signing/attestation/distribution, a complete product/native/OS/
codec/firmware SBOM and advisory review, applicable native sanitizers, custody,
intended-host restore, production group composition and physical qualification
remain open. Keep the program and related issues open until those gates are
actually satisfied. A weekly/PR native CI check detects newly published findings;
it does not silently update an installed gateway or authorize deployment.
