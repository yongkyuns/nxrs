# Actual Ubuntu gateway native package audit

Issue #38 requires native/distribution coverage beyond Cargo and Python package
audits. This collector builds the real host gateway plus FFmpeg publisher/reader,
then records SHA-256 for the actual binaries and their loader-resolved libraries.
It includes FFmpeg/ffprobe, nginx, OpenSSL, cryptsetup, systemctl and system Python, including
the TLS/hash extension images actually mapped by the system-Python collector.
`dpkg-query` maps each distribution file to its full binary/source package
revision, architecture and Ubuntu PURL. Debian epochs and revision suffixes are
preserved; upstream version banners alone cannot account for Ubuntu backports.

The scope is the disposable GitHub-hosted Ubuntu 24.04 amd64 qualification image,
not a remote scan or an inventory of a personal host. The collector refuses
personal hosts before querying packages or sending advisory requests. It makes
no package, filesystem, service, trust-store, disk or device changes. The separate
CI installer uses invocation-local signed official Ubuntu sources; it does not
change persistent package-source configuration. The inventory roots are fixed.
Repository-built binaries carry actual hashes and exact clean source inputs;
they are not misidentified as Ubuntu packages.

Current Ubuntu advisory data is queried through the official OSV API for every
resolved source package/version. All pagination, complete result counts, matching
source/release identities and Debian affected ranges are required. Lookup/schema
failures, missing/unowned libraries, mixed source versions and changed input
inventories refuse admission. Official withdrawn records (including tombstones with no remaining affected
packages) are retained separately and never counted as active vulnerabilities.
A metadata-only historical FFmpeg revision must
produce the actual Ubuntu CVE-2025-59734 finding. No affected control is installed
or executed for that control, and no exploit sample is downloaded.

The report records actual package-level findings without claiming that each is
reachable through nxrs's selected codecs/configuration. The currently observed
Ubuntu FFmpeg `7:6.1.1-3ubuntu5` revision has published findings in Ubuntu's data,
including CVE-2025-59734. The hosted job retains the original assessment, refreshes only the actual
inventoried package set from the signed official archives, rebuilds native
bindings against refreshed headers/libraries, then runs a fresh final gate.
The baseline step explicitly checks the completed assessment and expected exit
status; collection errors never become a successful baseline. The refresh receipt
is not security admission. Its package set must match the exact current checkout
and installed baseline. No personal host can run the updater.

The final gate must remain red until applicable findings are
remediated or an exact, justified, expiring configuration review is implemented;
there is no ignore list or success override. Native rebuild/minimization and applicability review remain candidate follow-ups.
Availability of a package update alone is not proof that it fixes a finding.

The sole public output is bounded to 1 MiB and contains source/binary/package
identities, advisory IDs/record hashes and fixed scope fields. It is retained for
seven days even when findings fail admission. No raw build/native output,
credentials, private paths or footage is uploaded. A red advisory result has
`qualified=false`. A completed inventory is retained with an explicit pending or
failed advisory status if later queries fail; missing findings are never a clean
scan. Collection failures do not fabricate an inventory.

This is an actual startup loader graph and system-Python TLS inventory. It does
not cover every delayed/dynamic plugin, statically embedded third-party source,
kernel, firmware, device, full release image or other distribution. Installed
file hashes and dpkg ownership are observations, not authenticated matching of
each file to a signed package archive or trusted release attestations. Full
native advisory closure, package-payload authentication, physical/FIPS and
deployment flags remain false even if this scoped advisory gate passes. Native
Python wheel and MediaMTX/backup-engine qualification remain separate origins.

Read-only host controls:

```sh
python3 -m unittest discover -s tests/host -p test_ubuntu_native_packages.py -v
```

The actual inventory command runs only in the guarded hosted job after its
required packages and binaries are present:

```sh
/usr/bin/python3 tools/native-packages/ubuntu.py --report /tmp/new-native-report.json
```

Primary references: [Ubuntu OSV format and source-package mapping](https://documentation.ubuntu.com/security/security-updates/osv/),
[Ubuntu backports and package identity](https://documentation.ubuntu.com/security/security-features/vex-sbom/),
[Ubuntu CVE-2025-59734](https://ubuntu.com/security/CVE-2025-59734).

## Minimized native candidate origins

`candidate.py` assesses the actual minimized gateway rather than projecting a
result by subtracting stock packages. The guarded hosted workflow first records
and refreshes the real distribution-linked gateway's package set. It then builds
the pinned, patched minimized runtime against that refreshed image, rebuilds the
actual SDK publisher/reader, and executes native decoder-observer, credential,
confinement and 96-frame clip controls. This order avoids qualifying media against
system libraries that are subsequently replaced by the package refresh.

The candidate requires the complete current native qualification proof before
inventory. Its twelve actual roots are publisher, reader, controller, recorder, the proxy
admission launcher, custom FFmpeg/ffprobe, nginx, OpenSSL, cryptsetup, systemctl
and system Python. The strict failure handler invokes systemctl, so its real
image and libraries belong in this graph too. Loader-resolved
custom images must match the qualified runtime bytes and explicit FFmpeg/x264
source identities. Ubuntu images retain their observed full dpkg binary/source
revisions; unknown or changed images refuse. The actual system-Python TLS/hash
mappings remain in scope. Every candidate distribution package must match the
executed refresh receipt exactly. No retained library or finding is subtracted
on the basis of an assumed dependency graph or version banner.

The new job queries current official Ubuntu OSV metadata for the actual candidate
source-package versions, with the same pagination, range, withdrawal and affected
negative controls. Applicable findings keep this scoped gate red. An incomplete
query retains a pending/failed receipt with `qualified=false`, never a clean
result. The independent broad stock-profile gate remains unchanged.

Custom FFmpeg/x264 images are identified as pinned-source builds, not mislabeled
as Ubuntu packages or assessed using Ubuntu backport ranges. Their exact source
and applied cleanup identities are retained, with custom-source advisory coverage
explicitly unassessed by this job. The x264 sanitizer workflow is a separate
narrow assessment. `qualified=true`, if achieved, describes only this actual
native inventory and its Ubuntu-package advisory scope. Complete upstream/native
advisory coverage, authenticated package payloads/release trust, dynamic plugins,
kernel/firmware, physical/FIPS and deployment permission remain false. Native
Python wheels and Go media/backup engines retain their separate origin gates.

The sole upload is bounded public JSON: stock baseline, refresh, runtime build,
executed native qualification/reader/isolation receipts and candidate assessment.
No binaries, native logs, source archives, generated keys or footage are uploaded.


## Authenticated mapped distribution payloads

`payloads.py` adds an independent, read-only check of the candidate's Ubuntu
images. It runs only on the guarded disposable Ubuntu 24.04 amd64 runner, after
an actual completed candidate assessment, including an assessment with findings.
It never installs packages, modifies package sources or system trust, extracts
archive paths, or executes downloaded payloads. The existing advisory exit status
is preserved after this check; authenticated vulnerable bytes still fail admission.

The reviewed, expiring `security/ubuntu-archive-trust.json` pins Ubuntu's 2018
archive primary key fingerprint. The image-supplied archive keyring is untrusted
input: even a world-writable source is not an authority. A bounded, regular,
unchanged source is copied without following symlinks into a fresh private GPG
directory and a read-only `0400` snapshot. GPG uses only that snapshot and must
report the pinned primary fingerprint on the actual valid signature. No system
keyring permission or trust-store change is needed. Only the authenticated
`InRelease` cleartext is parsed.
The chain requires Ubuntu/noble/suite identity, SHA-256 package index hashes,
exact binary and source versions including epochs, the indexed `.deb` size/hash,
its control identity, and every inventoried distribution image's bytes, owner,
group, mode and size in regular tar members. Selected symlinks, hardlinks,
duplicate or missing members and ambiguous paths refuse. Only verified system
usrmerge aliases normalize legacy `/lib`, `/bin` and related members. Installed
images and their parent directories must be root-owned and protected from group
or other writes, and their identity must remain stable throughout verification.
The payload parser uses the bounded seekable temporary file, avoiding Python
streaming-tar EOF handling (Ubuntu CVE-2026-11972). No tar extraction API is used;
this does not clear the Python package advisory gate.

Rolling metadata must be no more than seven days old; future dates beyond five
minutes and expired `Valid-Until` values refuse. The immutable noble base is
exempt from rolling age, but still needs the current pinned signer and unexpired
review policy. There is no insecure download or missing-version fallback. Downloads,
decompression, subprocess groups, private logs, file counts and elapsed time have
explicit limits. Raw metadata, packages and logs stay in the private runner work
directory. The eighth bounded public JSON receipt joins the exact candidate/source
hashes, signed releases, indexes, packages and matched images. Each live release
must reject an actual altered signature; each actual package must reject a
changed byte through the same digest admission predicate.

`qualified=true` in this receipt means only `distribution_payloads_authenticated`
for the inventoried mapped Ubuntu images. The overall `package_payload_authenticated`
flag remains false: custom FFmpeg/x264, repository executables, Python wheels,
Go engines, delayed plugins and the complete release are outside this receipt.
The candidate's advisory result and finding count remain explicit and unchanged.
This does not qualify post-install scripts/configuration, the verifier bootstrap,
archive key revocation/rotation, kernel, firmware, physical hardware, FIPS or
production deployment. In particular, GPG's verifier trusts its supplied keyring
and does not establish the key's revocation lifecycle; fingerprint pinning and a
short review policy do not replace that separate release-trust requirement.

Offline refusal controls:

```sh
python3 -m unittest discover -s tests/host -p test_native_payloads.py -v
```

The workflow runs the live verifier with `--candidate`, a new private `--work`
directory and a new public `--report` path. It requires no personal-host setup.
Primary references: [Ubuntu archive signing fingerprints](https://wiki.ubuntu.com/SecurityTeam/FAQ)
and [GPG verifier semantics](https://www.gnupg.org/documentation/manuals/gnupg/gpgv.html).

The read-only Debian payload parser preserves literal POSIX `\xHH` filename
sequences used by systemd units. It never decodes those bytes into path
separators/dots/NULs or extracts paths. Absolute/traversal/ambiguous names and
selected symlink/hardlink/duplicate aliases still refuse; every selected image
retains its exact signed archive, bytes, mode, owner and size checks.
