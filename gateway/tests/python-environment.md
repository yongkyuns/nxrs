# Frozen Python media qualification environment

Gateway API tests use a complete six-package lock in `requirements.txt`. The
26-package media identity lock in `media-requirements.txt` includes that graph and the
existing three-package enrollment crypto lock. The supported qualification
interpreter is **CPython 3.12 on Linux or macOS**. Hashes cover published wheels;
installation uses exact binary wheels, including two explicit pinned source rebuilds.

The previous aiohttp 3.12.15 pin has published advisories, including a
[native HTTP response-parser out-of-bounds read](https://github.com/aio-libs/aiohttp/security/advisories/GHSA-cq5v-8q36-5273).
The current graph pins aiohttp **3.14.5**, aiortc **1.15.0** and PyAV **19.0.2**.
Media installation uses the [shared native build and admission procedure](native-python.md),
including the upstream aiortc compatibility backport and SRTP/OpenSSL rebuild.
The [aiortc release](https://github.com/aiortc/aiortc/blob/1.15.0/docs/changelog.rst)
also hardens SCTP chunk parsing and permits PyAV 17, fixing a per-frame leak.
Enrollment cryptography 50.0.2, CFFI 2.1.1 and pycparser 3.11 remain shared inputs.
jsonschema remains 4.25.1; its previously floating transitive packages are frozen.

Create a fresh environment from the repository root:

```sh
python3.12 gateway/tools/native_python.py prepare --work /tmp/nxrs-media-build \
  --runtime /tmp/nxrs-media-test --qualify
/tmp/nxrs-media-test/bin/python -m pip check
/tmp/nxrs-media-test/bin/python gateway/tools/python_environment.py inventory --report /tmp/python-installed.json
NXRS_TEST_PYTHON_ENVIRONMENT=media /tmp/nxrs-media-test/bin/python -m unittest discover -s gateway/tests -v
/tmp/nxrs-media-test/bin/python gateway/tests/python_media_reference.py --inventory /tmp/python-installed.json --report /tmp/python-loopback.json
```

Use `requirements.txt`, group `core` and `NXRS_TEST_PYTHON_ENVIRONMENT=core` for
API-only tests. Keep auditor packages in a separate environment so they cannot
silently enlarge the application environment:

```sh
python3.12 -m venv /tmp/nxrs-media-auditor
/tmp/nxrs-media-auditor/bin/python -m pip install pip-audit==2.10.1
python3.12 gateway/tools/python_environment.py audit --audit-python /tmp/nxrs-media-auditor/bin/python --report /tmp/python-advisories.json
```

The gate checks both complete graphs against current official PyPI vulnerability
metadata, using a fresh owned cache. Its metadata-only collector skips nested requirements,
so the gate flattens the validated full graph with every version and hash before
collection, then independently requires the returned complete package set. Lookup errors, missing/duplicate/skipped
packages, changed versions, malformed results, findings and unexpected process
statuses refuse admission. It also queries the old affected aiohttp requirement
without installing it and requires the actual response-parser advisory. A real
pip download of a healthy wheel with a deliberately wrong hash must fail for the
hash mismatch. There is no advisory ignore list or automatic fix operation.

`python-environment.json` names the expected package graphs independently of the
requirements parser. Requirements may include only the three reviewed lockfiles;
every package must have an exact version and SHA-256 hashes. All Python gateway media integration workflows use the shared native builder, then install its complete generated hash lock. Direct pip installation of the media identity lock is unsupported. The dedicated
workflow refreshes advisories weekly and on relevant changes, and installs/tests
the media graph on Linux amd64 and macOS arm64.

The bounded installed report contains exact package versions, hashes of installed
wheel RECORD metadata, bootstrap package versions, CPython/OS identity and native
OpenSSL/PyAV version observations. It does not contain local paths or credentials.
Package RECORD hashes identify metadata; they do not verify every Python file or authenticated wheel publisher attestations. Native admission additionally verifies actual native payload bytes in the PyAV, SRTP and cryptography wheels. pip hash checking establishes
the downloaded wheel identity at installation. Bootstrap pip/setuptools/wheel are
listed separately and are outside these application-package advisory graphs.

SDK media and encrypted-composition receipts collect the actual fixture
interpreter inventory, bind its source/lock inputs and recheck it after media
execution. The loopback witness requires the compiled aiohttp parser and H.264
capability, decodes six advancing frames and joins peers, tasks and the owned
HTTP server. It is a dependency compatibility witness; it does not establish
native-parser sanitizer coverage or physical rendering performance.

Python package advisory results remain distinct from native coverage. Media admission adds the explicit selected-branch FFmpeg and OpenSSL checks and source rebuilds described in native-python.md. Full SRTP/transitive native-library and host OS advisory closure, production firmware, protected key storage, trusted release provenance and FIPS validation remain separate. Updating versions or wheel hashes requires a fresh resolution, complete
audits, installed inventory checks and the actual media/recording integrations.
