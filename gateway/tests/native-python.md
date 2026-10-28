# Shared Python native media qualification

The Python package gate does not cover every embedded native library. Actual
inspection of the former wheels found **FFmpeg 8.1.1** in PyAV 17.1.0 and
**OpenSSL 3.5.4** inside pylibsrtp 1.0.0, separately from cryptography's OpenSSL
4.0.3. The [FFmpeg security page](https://ffmpeg.org/security.html) lists fixes
for CVE-2026-8461 and CVE-2026-30999 in 8.1.2. The
[official OpenSSL record](https://openssl-library.org/news/secjson/cve-2026-35189.json)
includes 3.5.4 in its affected range. These are upstream version findings;
exploitability in nxrs's configuration has not been reproduced.

`native-python.json` pins the shared fixture environment: PyAV 19.0.2
(FFmpeg 9.0.2), SRTP 2.7.0 rebuilt against static OpenSSL 4.0.3, and aiortc
1.15.0 with exactly the upstream [PyAV compatibility backport](https://github.com/aiortc/aiortc/commit/10ccd168528d93996d013d0ecfae679c3a764b68).
Every media workflow uses this environment, including TLS ingestion, recording,
service/VPN recovery and encrypted recording/backup composition. The API-only
six-package environment remains independent of native media builds.

From a clean checkout with CPython 3.12, a C compiler, Perl and make:

```sh
python3.12 gateway/tools/native_python.py prepare --work /tmp/new-native-build \
  --runtime /tmp/new-media-runtime --qualify
/tmp/new-media-runtime/bin/python gateway/tools/python_environment.py inventory \
  --report /tmp/new-media-inventory.json
NXRS_TEST_PYTHON_ENVIRONMENT=media /tmp/new-media-runtime/bin/python \
  -m unittest discover -s gateway/tests -v
```

The work and runtime paths must be absolute and absent. The runtime contains
public installed code and bounded receipts; its files are readable by the
recording fixture's separate UID. Native sources, compiler output and auditor
state stay private in the build directory. No system package, library, service
or personal setting is replaced. Without `--qualify`, building alone does not
admit the environment to media fixtures. The separate `qualify` command can
finish such a build using an isolated pip-audit 2.10.1 interpreter and a new
report path; it also writes the runtime's admission sidecar.

The builder verifies source archive hashes, exact wheel hashes for build tools
and the complete application graph, applies only the one-line upstream aiortc
dependency change, runs SRTP self-tests, and installs the two locally built
wheels using direct owned wheel references and a generated hash lock. A plain
`--find-links` directory is insufficient: pip can prefer a same-version PyPI
wheel. Its wrong hash was observed to fail during preflight. Direct installation
of `media-requirements.txt` is unsupported: that file retains reviewed upstream
package identities for audits; the builder substitutes the mandatory rebuilt
aiortc/SRTP wheel hashes. Both rebuilds retain their upstream Python package
versions and are identified by the source/backport and wheel receipts.

Locally compiled wheel hashes bind observed outputs to the source/library
archive hashes. They are not independently authenticated release signatures
or a claim of identical output across compilers/hosts. The qualification report
records the actual clean checkout and all environment/workflow input hashes.

Native qualification reads the actual SRTP library's exported version functions,
cryptography's own crypto version and PyAV's FFmpeg version. It verifies actual
native payload bytes against wheel RECORD digests, including PyAV's bundled
shared libraries. It does not identify versions or audit all those transitive
libraries. The OpenSSL gate fetches every CVE JSON record in the current official
4.0 branch index and rejects unsupported records/ranges and findings. The FFmpeg
gate requires every security fix listed for a later selected-branch release;
that page is a fixes list rather than a complete affected-range database.
Unlisted and not-yet-backported issues require additional review. Metadata-only
controls require actual old-version findings without loading those libraries.
A fresh package audit checks the actual complete 26-package environment.

The real owned HTTP/DTLS/SRTP/H264 witness decodes six advancing synthetic frames
and joins peers, tasks and server. The runtime admission sidecar includes those
results, native identities/payload hashes and advisory records. Installed Python
inventories require the matching build/qualification pair, source inputs and
actual unchanged native bytes. SDK media receipts recheck that inventory after
execution; encrypted composition validates the same bounded evidence with
stdlib Python, without importing native media into its outer root helper.
Separate full native reports are retained in the main public artifacts and
joined through their SHA-256. No receipt flag can substitute for full recording,
TLS, restore or ownership controls in those integration fixtures.

Build/process output is bounded and descendants retire on failure/deadline.
CI retains bounded JSON only for seven days. Full native/transitive/OS advisory
closure, trusted release provenance, firmware/physical hardware, FIPS validation
and deployment admission remain false. A native loopback alone does not qualify
the full gateway composition; its separate executed receipt supplies that scope.
