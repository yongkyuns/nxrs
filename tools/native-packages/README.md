# Actual Ubuntu gateway native package audit

Issue #38 requires native/distribution coverage beyond Cargo and Python package
audits. This collector builds the real host gateway plus FFmpeg publisher/reader,
then records SHA-256 for the actual binaries and their loader-resolved libraries.
It includes FFmpeg/ffprobe, nginx, OpenSSL, cryptsetup and system Python, including
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
including CVE-2025-59734. The gate must remain red until applicable findings are
remediated or an exact, justified, expiring configuration review is implemented;
there is no ignore list or success override. Native rebuild/minimization and
current-package upgrades are candidate remediations, not completed work.

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
