# Isolated Python native media replacement candidate

The Python package gate does not cover every embedded native library. Actual
inspection of the locked macOS wheels found **FFmpeg 8.1.1** in PyAV 17.1.0 and
**OpenSSL 3.5.4** inside pylibsrtp 1.0.0, separately from cryptography's OpenSSL
4.0.3. The [FFmpeg security page](https://ffmpeg.org/security.html) lists fixes
for CVE-2026-8461 and CVE-2026-30999 in 8.1.2. The
[official OpenSSL record](https://openssl-library.org/news/secjson/cve-2026-35189.json)
includes 3.5.4 in its affected range. These are upstream version findings, not
claims that nxrs has reproduced an exploitable condition in its configuration.

`native-python.json` pins an isolated replacement experiment: PyAV 19.0.2
(FFmpeg 9.0.2), SRTP 2.7.0 rebuilt against static OpenSSL 4.0.3, and aiortc
1.15.0 with exactly the upstream [PyAV compatibility backport](https://github.com/aiortc/aiortc/commit/10ccd168528d93996d013d0ecfae679c3a764b68).
The existing application Python media lock and integration paths are unchanged.
This candidate must pass Linux/macOS native qualification and full recording,
TLS, service and encrypted-composition integration before replacing those paths.

From a clean checkout with CPython 3.12, a C compiler, Perl and make:

```sh
python3.12 gateway/tools/native_python.py prepare --work /tmp/new-native-media
python3.12 -m venv /tmp/new-native-auditor
/tmp/new-native-auditor/bin/python -m pip install pip-audit==2.10.1
/tmp/new-native-media/runtime/bin/python gateway/tools/native_python.py qualify \
  --audit-python /tmp/new-native-auditor/bin/python --report /tmp/new-native-report.json
```

The builder verifies official source archive hashes, exact wheel hashes for
build tools and the complete application graph, applies only the one-line
upstream aiortc dependency change, runs SRTP's upstream self-tests, and installs
the two locally built wheels using a generated hash lock. Public wheel hashes
remain reviewed input identities; locally compiled wheel hashes are observations
bound to source and library archive hashes, not independently trusted release
signatures or cross-host reproducibility claims. Neither source rebuild changes
the upstream Python package version; the receipt identifies the modifications.
All libraries and virtual environments remain inside the new owned directory;
no system package, service or personal configuration is changed.

The native check reads the actual SRTP library's exported version functions,
cryptography's own crypto version and PyAV's FFmpeg version. It hashes every
native payload belonging to those three wheels and checks actual bytes against
wheel RECORD digests. This includes dependent shared libraries bundled by PyAV;
it does not identify versions or audit all of those transitive native libraries.
The OpenSSL gate fetches every CVE JSON record named by the current official 4.0
branch index; it rejects unsupported records, unknown ranges and findings. The
FFmpeg gate requires every security fix listed for later releases on the selected
branch. FFmpeg's page is a fixes list, not a complete affected-range database;
unlisted and not-yet-backported issues require additional review. Metadata-only
controls require the old FFmpeg and OpenSSL versions to be refused without
loading those old libraries. Current package advisories cover the actual
26-package candidate graph; no advisory is suppressed.

The existing owned HTTP/DTLS/SRTP/H264 witness decodes six advancing synthetic
frames and joins both peers, tasks and server. It accepts an explicit candidate
inventory collector; its ordinary CLI continues to require the original media
lock. Private build logs are bounded; descendant processes are retired on
failure/deadline. CI uploads only the bounded JSON receipt for seven days.

The receipt explicitly leaves full native/transitive/OS advisory closure,
existing integration replacement, firmware, physical hardware, trusted release
provenance, FIPS validation and deployment admission false. The source recipe
and a passing host experiment cannot close those release gates.
