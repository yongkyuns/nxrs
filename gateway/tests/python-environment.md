# Frozen Python media qualification environment

Gateway API tests use a complete six-package lock in `requirements.txt`. The
26-package media lock in `media-requirements.txt` includes that graph and the
existing three-package enrollment crypto lock. The supported qualification
interpreter is **CPython 3.12 on Linux or macOS**. Hashes cover published wheels;
installation accepts binary wheels only and never falls back to a source build.

The previous aiohttp 3.12.15 pin has published advisories, including a
[native HTTP response-parser out-of-bounds read](https://github.com/aio-libs/aiohttp/security/advisories/GHSA-cq5v-8q36-5273).
The replacement pins aiohttp **3.14.5**, aiortc **1.15.0** and PyAV **17.1.0**.
The [aiortc release](https://github.com/aiortc/aiortc/blob/1.15.0/docs/changelog.rst)
also hardens SCTP chunk parsing and permits PyAV 17, fixing a per-frame leak.
Enrollment cryptography 50.0.2, CFFI 2.1.1 and pycparser 3.11 remain shared inputs.
jsonschema remains 4.25.1; its previously floating transitive packages are frozen.

Create a fresh environment from the repository root:

```sh
python3.12 -m venv /tmp/nxrs-media-test
/tmp/nxrs-media-test/bin/python -m pip install --require-hashes --only-binary=:all: -r gateway/tests/media-requirements.txt
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
every package must have an exact version and SHA-256 hashes. Install commands in
all Python gateway integration workflows require hashes and wheels. The dedicated
workflow refreshes advisories weekly and on relevant changes, and installs/tests
the media graph on Linux amd64 and macOS arm64.

The bounded installed report contains exact package versions, hashes of installed
wheel RECORD metadata, bootstrap package versions, CPython/OS identity and native
OpenSSL/PyAV version observations. It does not contain local paths or credentials.
RECORD hashes identify metadata; they are not a verification of every installed
file or authenticated wheel publisher attestations. pip hash checking establishes
the downloaded wheel identity at installation. Bootstrap pip/setuptools/wheel are
listed separately and are outside these application-package advisory graphs.

SDK media and encrypted-composition receipts collect the actual fixture
interpreter inventory, bind its source/lock inputs and recheck it after media
execution. The loopback witness requires the compiled aiohttp parser and H.264
capability, decodes six advancing frames and joins peers, tasks and the owned
HTTP server. It is a dependency compatibility witness; it does not establish
native-parser sanitizer coverage or physical rendering performance.

Python package advisory results do not cover every FFmpeg, SRTP, OpenSSL or other
native library embedded in wheels, the host OS, production firmware, protected
key storage, trusted release provenance or FIPS validation. Those gates remain
separate. Updating versions or wheel hashes requires a fresh resolution, complete
audits, installed inventory checks and the actual media/recording integrations.
