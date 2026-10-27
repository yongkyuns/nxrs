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

The separate **Python TLS callback backport experiment** authenticates that same
Python source chain and applies all 82 distribution patches without fuzzy matching.
The distribution's `debian/rules` requires regenerating `configure` with Autoconf
2.71 before configuration; the experiment records that generated input and builds
a fresh baseline in disposable hosted Ubuntu. It then applies the reviewed
CPython 3.12 callback/table backport and rebuilds the SSL extension.
The distribution XML backport uses private CPython headers. The experiment follows
the authenticated minimal-module builtin list and `--with-system-expat` selection
from `debian/rules`, retaining the actual core-builtin context. SSL stays shared.
Both builds must import the XML builtin, parse an ordinary document and retain
the interpreter and mapped SSL/crypto/XML library hashes. The setup profile and
its authenticated source-input hashes are separately recorded.
The patch's upstream origin and CPython license are retained beside it.
The interpreter and actual mapped OpenSSL library hashes must stay unchanged.
For TLS 1.2 and 1.3, ordinary fixture chains must still exchange application data;
the public upstream compromised-chain fixture must be accepted by the baseline
and refused with verification code 28 by the patched module in both required and
optional verification modes. Disabled verification is not an admitted mode.

The baseline is a fresh source build. Correspondence with the distribution's
installed SSL module remains a separate gate. The experiment installs no runtime
or trust material and enables no gateway selection. Its fixed upstream test
archive includes public test keys; it stays in the private build directory along
with source files and compiler output. Process groups must retire and that
directory must be removed before success is published. The sole uploaded output
is an unsigned public JSON receipt, at most 64 KiB, retained for seven days.
Failed builds expose fixed categories, an exit status, numeric Makefile locations
and normalized source locations/quoted identifiers admitted only from an allowlist
of the authenticated public C/header sources captured before native execution.
Arbitrary compiler text and private/absolute paths stay private.
Witness startup/refusal evidence uses a closed module-name/error/category schema;
the standalone TLS witness loads archive and build helpers only in the parent.
Isolated mode stays enabled. The witness explicitly admits the owned extension
directory named by the source build's bounded `pybuilddir.txt` and verifies that
the SSL image comes from that directory and XML is builtin to the exact measured
interpreter; the extension-directory config hash is joined.
The candidate callback result cannot clear a distribution advisory or admit
bootstrap, key lifecycle, physical, FIPS, gateway integration or release scope.

```sh
# Disposable hosted Ubuntu only; source/build/fixture cleanup precedes publication:
python3 tools/native-packages/python_tls.py qualify \
  --work "$RUNNER_TEMP/python-tls-build" --report "$RUNNER_TEMP/python-tls-candidate.json"
```
