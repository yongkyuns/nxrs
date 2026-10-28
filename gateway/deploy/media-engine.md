# Reproducing the media runtime

The gateway and native client reference tests use **MediaMTX 1.21.2-nxrs.1**.
This is upstream v1.21.2 at commit `914e39535ece709919ac325aaeed98d394ed9093`,
rebuilt with Go 1.27.3 and `golang.org/x/net` v0.60.0. These updates address the
imported certificate trust and HTTP/2 advisories detected in the upstream Go
1.27.2 / x/net v0.59.0 executables. See the official
[Go certificate advisory](https://pkg.go.dev/vuln/GO-2026-6809) and
[HTTP/2 advisory](https://pkg.go.dev/vuln/GO-2026-6603).

[`dependencies.json`](dependencies.json) is the single recipe for Linux amd64,
macOS arm64 and macOS amd64. It pins the source archive, Go SDKs, scanner module,
frozen `mediamtx/go.mod` and `go.sum`, full compiled module graph, executable
checksums, and the original hls.js browser asset. No MediaMTX source behavior is
patched. Builds retain symbols, disable CGO and VCS stamping, and use trimpath.
The artifact version explicitly identifies the nxrs rebuild.

From the repository root, with Python 3.12+ and network access to the public
Go proxy/checksum service, advisory database and pinned upstream archives:

```sh
python3 gateway/tests/fetch_mediamtx.py --artifact linux_amd64 --output /tmp/nxrs-mediamtx --qualify
python3 gateway/tools/verify_media_evidence.py /tmp/nxrs-mediamtx --require-qualification
```

Omit `--artifact` for the host architecture. Optional `--cache DIRECTORY` caches
public archives by SHA-256; every reuse verifies the bytes. Outputs must be new;
existing executables, sidecars, symlinks and FIFOs are refused before build work.
There is no fallback to an upstream executable when a build or audit fails.
These commands create local artifacts only. Installation remains a separate
manual operation in the [deployment instructions](README.md).

Each build runs fresh `govulncheck` v1.9.0 source and binary scans against
`https://vuln.go.dev`. Imported or called affected packages refuse admission.
Unimported module-only findings remain visible in the report, including the
unmaintained `x/crypto/openpgp` advisory when that package is absent from the
compiled/imported program. No advisory ignore rule is added.

Go represents this rebuilt main module as `(devel)`. A separate current Go
module-index check therefore requires an explicit reviewed main-module advisory
set and exact OSV bytes. The existing [MediaMTX advisory](https://pkg.go.dev/vuln/GO-2026-4989)
is fixed before v1.21.2. Its review expires on **2026-11-23**; new, changed, removed,
expired or affected advisory records fail closed until maintainers update the
review. Semantic ranges outside the reviewed introduced/fixed scheme also refuse.

`--qualify` scans the checksum-pinned original affected executable and requires
actual certificate-trust and HTTP/2 findings. That executable is never executed.
On a matching host, qualification also executes eight named upstream
authentication/configuration tests and the replacement runtime version command.
Cross builds record `target_executed: false`; the Intel macOS CI case runs on an
arm64 runner and supplies cross-build evidence only.

The executable has three public sidecars: CycloneDX 1.6 `.cdx.json`, bounded
`.advisories.json`, and unsigned in-toto/SLSA-shaped `.provenance.json`. The verifier
binds these to the exact artifact, six source/recipe/module inputs, SDK and asset,
and all 84 compiled modules. Coherently rehashing an incomplete inventory still
fails its separately pinned module graph. These records provide reviewable
integrity evidence; they are not authenticated publisher attestations or a formal
SLSA certification. Existing integration workflows build and audit this runtime;
SDK media and encrypted composition receipts additionally bind its verified
inventory/advisory hashes and stage only its public sidecars.

The dedicated `media-native-security.yml` workflow builds all three artifacts,
runs real refusal controls and coherent evidence tampering tests, and retains
only the reviewed executable and public evidence. Its weekly run detects new
advisories and expired reviews. An offline sidecar verifier checks integrity and
review expiry; it does not refresh the advisory database.

These Go gates do not qualify hls.js JavaScript advisories, the OS, other native
libraries, physical hardware, protected device keys, trusted release signing or
FIPS validation. Those claims remain false in the evidence. A release must
refresh the native audit and satisfy the separate product acceptance gates.
