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
inventory. Its fourteen actual roots are publisher, reader, controller, recorder, the proxy
admission launcher and installer, the read-only VPN startup admission helper, custom
FFmpeg/ffprobe, nginx, OpenSSL, cryptsetup, systemctl and system Python. The new
Rust helper is an actual separately hashed repository executable; its resolved
libraries remain in the same distribution advisory/payload inventory. The strict failure handler invokes systemctl, so its real
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


## Shared PCRE2 qualification candidate

`pcre.py` is a disposable hosted-Ubuntu qualification recipe. It reuses the
exact PCRE2 10.49 archive, publisher signature/key and complete expiring upstream
advisory review in `security/gateway-proxy-dependencies.json`. It builds the
8-bit shared library with Unicode and JIT, runs all four upstream test suites,
requires the original exported API availability/version requirements and SONAME,
pins the resolved ELF version map produced by configure from its signed template,
and executes an ordinary
interpreter/JIT/loaded-image probe. The native witness uses no malicious pattern.

Only after those controls pass does the recipe atomically replace the existing
`libpcre2-8.so.0` symlink with a link into its sealed root-owned qualification
prefix. It never truncates an existing ELF image. The actual cryptsetup/systemctl
linkage and normal version commands must select the new image. On a handled
failure it restores the original link only if the current link still belongs to
this operation and the saved original image is unchanged; it invalidates any
published success receipt. Process or machine loss is not a qualified production
installation transaction.

The kernel VPN, real strict systemd and encrypted-volume workflows run the
recipe after their native package installation. Their fixtures recheck the
root-owned receipt, source identity, selected link and library hash before and
after their actual operations. Encrypted compositions bind its two digests in
each case and retain the full bounded public source receipt as a fourteenth JSON
record. Three cases must use the same selected library/receipt. No source trees,
private work paths, raw logs, keys, volumes or footage are uploaded by this recipe.

The candidate inventory classifies only the exact qualified shared-library image
as pinned-source PCRE2. Other loaded distribution images retain their package
owners, fresh Ubuntu advisory queries and signed-package payload verification.
The original Ubuntu PCRE2 package remains installed, and already-running
processes (including PID1/journald) may retain older mappings. This candidate
therefore cannot close the whole-OS/package, bootstrap, signing-key lifecycle or
release gates. Production packaging, cold-boot admission and review remain open;
no personal-host installation or production deployment is authorized here.

The shared Linux recipe and all operational qualifications are pending until
this exact review revision's hosted runs and independently joined reports pass.
The private macOS shared/JIT prototype is separately labeled and is not Linux ELF
or systemd/encrypted-storage evidence.


### Package installation candidate

`pcre_package.py` assembles the already source-qualified PCRE2 shared image into
an Ubuntu amd64 `libpcre2-8-0` candidate, version `10.49-0nxrs1`. The runtime
package owns the versioned ELF and relative SONAME link in the standard multiarch
directory, signed-source license and qualification receipt, md5 manifest, shlibs
declaration and `ldconfig` trigger. Its libc dependency comes from the actual
ELF requirements. Two independent package staging/assembly operations must yield
identical archive bytes; this does not prove two full source builds reproducible.

The guarded `gateway-pcre-package.yml` job creates a fresh signed-archive Ubuntu
userspace using a single signed Noble suite, then applies current signed
updates and installs its system tools through APT inside that root. This avoids
debootstrap1.0.134's documented multi-suite pre-dependency resolver bug
([Debian#1024870](https://bugs.debian.org/cgi-bin/bugreport.cgi?bug=1024870))
without omitting updates or ignoring dependency errors. Chroot service starts
remain prohibited. The exact current vendor library digest and exported ABI are
recorded separately from the host source witness's old-library identity. It uses actual
dpkg installation and ownership, refuses damaged archives, distinguishes unpacked
from configured state, resumes configuration and checks idempotent installation.
Fresh cryptsetup/systemctl processes and the interpreter/JIT image probe use the
packaged library. Synthetic LUKS header format, accepted/refused key and header
backup/restore controls preserve the encrypted-volume ABI boundary. A deliberately
restored vendor package and a changed library both fail candidate admission;
reinstalling the exact candidate restores it. Small KDF settings belong solely
to this named synthetic case. No device mapping or media integration is claimed
by these header controls; the separate recording composition retains that scope.

Only a strictly classified public package receipt of at most16KiB and its
existing public signed-source receipt of at most32KiB are uploaded. Package
archives, root trees, build logs, synthetic keys and volume/header bytes are not
uploaded. There is no apt repository publication, deployment or host installer.
An isolated chroot is not a system boot. Power-loss handling, protected downgrade
prevention, full source-build reproducibility, complete source/debian packaging,
release signing/key lifecycle, whole-OS updates and physical product acceptance
remain open. Successful candidate recovery does not authorize a production
rollback to the known older vendor library. Personal machines refuse execution
before any files, subprocesses, network or privileged actions.


### Virtual package cold boots

`package_boot.py` extends the package qualification with five independent QEMU
TCG kernels booting one persistent generated Ubuntu ext4 image: two installed
boots, an unconfigured package, vendor rollback and a changed nonloaded ELF byte.
A fixture-only systemd pre-start admission check refuses each invalid state
before its consumer runs; the driver recovers using the exact package and joins
the consumer. The first boot creates one owned encrypted volume using the
guest's real device-mapper. Each later boot opens that same retained volume and
checks the previous boot's payload before writing its own. Every case closes the
mapping, rejects a wrong key, reopens and verifies the current payload, then joins
the mapping. The private synthetic key remains inside the generated disk; this
does not qualify production key custody. The small KDF budget is explicitly
synthetic. Boot IDs must differ; one machine
identity and an fsynced plain ledger must persist. Read-only filesystem checks
require clean shutdown and exact disk-hash continuity between emulator processes.

The host refuses personal machines before side effects. Kernel preparation uses
a private mount namespace, and the raw filesystem is assembled only into a new
private regular file. QEMU runs unprivileged with no network, shared host tree,
monitor, physical disk or device passthrough. Keys, raw disks, initramfs, serial
logs, source package archives and volume bytes stay private. Only bounded strict
public boot/package/source receipts are retained after every case passes.

This is a direct-kernel virtual boot experiment, not production gateway admission,
UEFI/MCU verified boot, protected rollback, power-loss or physical qualification.
The fixture pre-start policy and plain ledger do not establish a production trust
root. Emulator/kernel/whole-OS advisory and release-signing/key lifecycle gates
remain open. No personal host kernel, service, trust store or hardware is changed.


The encrypted service boot experiment (`service_boot.py`,
`gateway-service-boot.yml`) extends the package-only VM fixture to the actual
generated control, media, HTTPS proxy and encrypted recorder group. Four fresh
QEMU TCG kernels share one private 8 GiB root image (4 vCPUs, 2 GiB RAM): new
provisioning, a reboot, a boot with the synthetic unlock key withheld, and a fresh
boot after that key is restored. The group must start automatically, retain the
exact first recorded clip and viewer denial, refuse the locked boot without a
plain recording directory, and join its service cgroups, mount, mapper and loop
before orderly shutdown. Unique kernel identities, a stable machine identity,
an fsynced generation ledger, clean filesystem checks and a disk hash chain bind
the four observations.

The guest receives only bounded committed source blobs, not Git configuration,
hooks, credentials, untracked build state or submodule contents. The signed
parent proxy build proof is retained; all four HTTPS boundary categories execute
again against the guest's actual loader/libc/OpenSSL images before generating
the fixture's startup approval. Application sandbox/dependency policies reuse the
production generators. Network/unlock providers and video are explicitly named
fixtures. The image has no NIC, host share, monitor or physical passthrough.

Only a complete strict public JSON receipt and its explicit public parent proofs
are uploaded. Images, keys, tokens, source archives, media and raw logs stay
private. A draft implementation or host regression pass is not evidence that
the four boots passed. This experiment does not qualify device TLS, actual VPN,
production unlock custody, power loss, physical storage, verified boot, protected
rollback, full OS/native advisory closure, FIPS or deployment.


The shared signed-source extractor preserves bounded archive timestamps along
with the admitted file bytes. Release-generated Autotools files must remain
newer than their inputs; extraction order cannot select an unreviewed local
Autotools regeneration. Archive paths, types, duplicates, size limits and
private permissions retain their original admission checks. A GNU Make4 host
witness verifies release ordering and confirms a genuinely changed input still
requires regeneration. Actual Expat GNU Make4 build/testing and Linux execution
remain separately qualified.

## Signed Expat candidate for system Python

The verified Ubuntu candidate loads source-package `expat` through system Python.
The isolated `gateway-system-xml.yml` workflow qualifies the pinned Expat 2.9.0
archive and detached signature against the maintainer's separately published key.
The certificate is pinned to the renewed snapshot at the maintainer's
[published keyserver location](https://github.com/libexpat/libexpat/issues/397#issuecomment-609777835),
SHA-256 `48a06f7b782958ecbab278a54581a92102a8b003bcf85fea4df2d9154699c2ba`.
Its primary fingerprint remains `3176EF7DB2367F1FCA4F306B1F9B0E909AF37285`.
The previous website snapshot expired in January 2026; the renewed primary and
signing-subkey certifications expire in December 2026. This is a reviewed
certificate refresh, with the same pinned release and signing key. GnuPG must
still verify the actual release and reject changed bytes, expired or revoked
keys, unexpected signers and weak digests before extraction. Later certificate
renewals require a new reviewed pin; no live keyserver refresh is admitted.
It runs the complete configured upstream suite with a nonzero check count,
preserves every vendor export and SONAME, and compiles an ordinary C API witness
against the actual Ubuntu headers. Wrong images and the old version must refuse.
The C witness and actual system Python must load the exact selected image and
exercise fragmented namespace/UTF-8 parsing and malformed-document refusal.
Selection is rolled back to the retained vendor image, checked, and restored to
the candidate before the bounded component receipt can be published. Every
child process joins through the existing deadline-controlled runner.

This workflow runs only in disposable GitHub-hosted Ubuntu 24.04 amd64 images.
The personal-host guard precedes policy reads, downloads, native commands and
mutation. Raw logs, SDK trees and images stay private; only `qualification.json`
is uploaded after successful component checks. Failure diagnostics expose only
bounded public source coordinates and fixed error classes.

The pinned release changelog documents fixes for CVE-2026-102633, CVE-2026-77214 and
CVE-2026-93990. It does not establish a fix for CVE-2025-66382, which is retained
explicitly as unresolved. The current upstream issue also records unresolved
reports; its checkbox count is not a confirmed vulnerability count. A passing
source/ABI/Python receipt keeps its upstream advisory gate, whole-native closure,
installed Ubuntu-package remediation, already-running processes, custom gateway
Python, release trust, physical/FIPS and deployment flags false.

The minimized native workflow now performs the component qualification after
the distribution refresh. The inventory then checks the current audit
interpreter's Expat version, parses a namespace document, and requires its mapped
Expat set to contain exactly the selected candidate image and bytes. A prior
child's successful receipt alone cannot admit an old or mixed current mapping.
The source-built image retains its source identity in the full native inventory.

Candidate schema 2 keeps distribution advisory results separate from explicitly
unresolved source findings. CVE-2025-66382 remains attached to the actual Expat
source version and image hash; dropping the Ubuntu package from the executed
graph cannot remove that finding. Even a clean distribution subset cannot admit
this unresolved source image. Distribution payload schema 2 carries the same
finding and component-receipt digest, verifies the current XML image before and
after its work, and preserves the advisory refusal independently of a successful
archive-payload check. Other source/release gates remain separate and open.

This wiring still requires actual hosted source/ABI/Python/native-graph evidence;
host regressions do not establish that it passed. The encrypted-recording
workflow also builds this candidate before volume allocation. Schema 2 volume
receipts recheck its exact current image across all three complete compositions;
mixed XML cohorts cannot publish an aggregate. The generated service-group
receipt binds the component proof and rechecks it after mount-loss recovery and
cleanup. These coupled checks still require actual hosted execution. Virtual
cold-boot integration/qualification of the replacement remain pending. Earlier verified
native/recording graphs retain their exact package findings and image scope.

Read-only host controls:

```sh
python3 -m unittest discover -s tests/host -p test_system_xml.py -v
python3 -m unittest discover -s tests/host -p test_candidate_native_packages.py -v
python3 -m unittest discover -s tests/host -p test_native_payloads.py -v
```

Primary references: [the pinned release and changelog](https://github.com/libexpat/libexpat/releases/tag/R_2_9_0),
[the maintainer's signing-key publication](https://github.com/libexpat/libexpat/issues/397#issuecomment-2496098377),
[upstream unresolved reports](https://github.com/libexpat/libexpat/issues/1160),
and [Ubuntu's deferred CVE-2025-66382 record](https://ubuntu.com/security/CVE-2025-66382).
