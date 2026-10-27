# Exact Ubuntu source provenance for applicability review

Issue #123's systemd review inspected the upstream and Debian archives for
`255.4-1ubuntu8.18`, but their unauthenticated descriptor was insufficient to
establish distribution provenance. The named `systemd-journald-wall` experiment
now authenticates those exact reviewed bytes through the existing expiring
Ubuntu archive authority: pinned signer, actual GnuPG verification, fresh signed
Release, SHA-256-bound Sources index, and every declared source file. It refuses
a missing exact version, different review bytes, duplicate source identity,
unsafe filenames, changed indexes/archives and invalid signatures.

This uses [Debian repository source indexes](https://wiki.debian.org/DebianRepository/Format)
and the [Debian source control/checksum contract](https://www.debian.org/doc/debian-policy/ch-controlfields.html).
The archive signature authenticates the descriptor's bytes; the descriptor's
own maintainer signature is not independently verified. Neither this receipt
nor source authentication establishes binary correspondence, affected-code
reachability, configuration, a mitigation, or resolution of CVE-2026-40228.
Those require separate exact source/binary/configuration/runtime evidence.

The named `python312-tls-distrust` case authenticates the original and Debian
patch archives for `python3.12` version `3.12.3-1ubuntu0.17`. This matches the
source-version metadata of the retained native cohort. It is a prerequisite to
reviewing the packaged SSL module against the
[CPython 3.12 certificate-distrust backport](https://github.com/python/cpython/pull/160571)
for [CVE-2026-115557](https://ubuntu.com/security/CVE-2026-115557).
The case does not apply that patch, rebuild or replace Python, inspect the
loaded SSL module, change a trust store, or clear any advisory. A preliminary
local archive inspection remains unverified until the signed source chain passes.

The **Ubuntu source provenance qualification** PR check runs both named cases
in isolated jobs with distinct artifact names. It can also be dispatched manually after the
workflow exists on the repository's default branch. The normal native audit is
unchanged. The source case uses only disposable hosted Ubuntu 24.04, downloads into a private fresh work
directory, and never installs or extracts the source files. It changes no
personal host or trust store. Signature-tool installation is confined to the
disposable runner; source authentication itself changes no packages or services.
Each job's sole uploaded output is a
bounded 64 KiB public JSON receipt retained for seven days; logs, archives and
source contents remain private to the disposable job. Failed provenance remains
failed and cannot publish source admission.

```sh
python3 -m unittest discover -s tests/host -p test_native_source_payloads.py -v
# Hosted Ubuntu qualification only:
python3 tools/native-packages/source_payloads.py --case systemd-journald-wall \
  --work "$RUNNER_TEMP/systemd-source" --report "$RUNNER_TEMP/systemd-source.json"
python3 tools/native-packages/source_payloads.py --case python312-tls-distrust \
  --work "$RUNNER_TEMP/python312-source" --report "$RUNNER_TEMP/python312-source.json"
```

The receipt binds the exact clean checkout and imported native-evidence source
inputs. All applicability, installed-binary, full-native, bootstrap, key-lifecycle,
physical, FIPS and deployment flags remain false. This completes a provenance
prerequisite; the original advisory and release acceptance gates remain open.
