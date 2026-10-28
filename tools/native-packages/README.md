# Actual Ubuntu gateway native package audit

Issue #38 requires native/distribution coverage beyond Cargo and Python package
audits. This collector builds the real host gateway plus FFmpeg publisher/reader,
then records SHA-256 for the actual binaries and their loader-resolved libraries.
The repository roots include the Rust camera ingest and authenticated-backup
executables as well as the controller, recorder, publisher/reader, proxy tools
and VPN admission. A missing ingest or backup executable refuses collection
before any loader or package query; neither can be inferred from another Rust
binary's similar library list.
It includes FFmpeg/ffprobe, nginx, OpenSSL, cryptsetup, systemctl and system Python, including
the TLS/hash extension images actually mapped by the system-Python collector.
That Python TLS observation includes the collector's own HTTPS advisory queries
and qualification/setup tooling. It does not establish that the Rust camera
ingestion path uses CPython TLS. Package-level matches remain retained while
runtime applicability is reviewed; collecting a tool dependency does not by
itself justify replacing the deployed gateway runtime or maintaining a custom
Python package. Rust certificate transport and the native tooling graph have
separate qualification requirements.
`dpkg-query` maps each distribution file to its full binary/source package
revision, architecture and Ubuntu PURL. Debian epochs and revision suffixes are
preserved; upstream version banners alone cannot account for Ubuntu backports.

Each root and library also records its byte size and ELF header identity alongside
its SHA-256. The header and hash come from one bounded regular-file read; inode,
size, mode and modification/change timestamps must remain stable, and the path
must still name that inode. Root/loader aliases are resolved first; a terminal
symlink introduced after resolution or a special file refuses. Each image is at
most 512 MiB. Before loader queries, every root must have a 64-bit little-endian
amd64 ELF executable/shared-object header; libraries must meet the same rule.
System V and GNU OS ABI values are accepted with ABI version zero. Numeric
machine/type/ABI/flags fields follow the [ELF header specification](https://gabi.xinuos.com/elf/02-eheader.html)
and [assigned machine values](https://gabi.xinuos.com/elf/a-emachine.html).
This is header identification, not complete ELF validation or CPU-feature
qualification. Package labels alone cannot establish an individual image's
architecture. The separate signed-package payload proof joins the exact file
hash and size; this observation does not itself authenticate the package.
No advisory is suppressed or declared unreachable by these architecture fields.

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

### System Python distribution metadata

The minimized audit also runs `python_components.py` with `/usr/bin/python3 -I`
after the completed candidate and authenticated-payload assessments. It joins
the interpreter's stable ELF/hash/size identity to that candidate's actual Python
root and retains one bounded `system-python-components.json`, even when the
unchanged advisory gate is red. The candidate receipt and exact clean source
inputs are rechecked before publication.

This collector observes distribution names/versions and stable metadata/RECORD
file hashes in the isolated interpreter's admitted system startup directories.
Schema 3 retains each distinct metadata path as a distribution instance with its
normalized name, version and observed hashes. Multiple installed copies, including
different versions of one normalized name, remain separate sorted entries;
`duplicate_names` explicitly identifies their ambiguity. Import precedence remains
unqualified. Repeated paths, duplicate identity headers, malformed identities,
links/special files, unprotected paths, mutation and resource-budget violations
refuse collection. The unchanged limit of 512 instances applies across all names,
so repeated names cannot bypass the count budget.

Parsing uses the [PyPA metadata format's `compat32` policy](https://packaging.python.org/en/latest/specifications/core-metadata/).
Some legacy packages have an unfolded continuation after the optional Keywords
header. Observe only this specific separator defect when Name and Version are
already unique, valid headers and the unparsed tail contains no further identity
header. Each instance explicitly retains `metadata_defects`, including the
separator defect when present. Other parser defects and ambiguous identities
still refuse. Raw metadata hashes remain unchanged; complete collection describes
the bounded observations, with package contents, format compliance, import
resolution and every qualification gate still unqualified.
Only fixed system paths, identities and hashes are public; package descriptions
and arbitrary metadata text are not exported. Collection does not import the
observed distributions, install software, change trust or alter an advisory
disposition. System Python startup hooks remain outside its coverage.

A header/identity refusal may emit one `PYTHON_METADATA_REFUSAL` JSON diagnostic,
bounded to 2 KiB. Its fixed schema contains only the protected public system path,
unchanged file hash/size, a fixed reason, identity-header counts and fixed defect
categories. Header values, descriptions and exception messages are withheld.
The diagnostic parser does not admit metadata: the original strict refusal still
terminates collection and no success receipt is written. Private, unprotected or
changed files produce no diagnostic.

The result has `collection_status=complete` and `qualified=false`. It is not an
authenticated inventory of package contents or proof of module absence: local
files without distribution metadata, import hooks, zip imports, application
environments and a complete deployed Python graph need separate evidence.
`RECORD` is observed metadata, not an independently authenticated wheel or a
verification of every installed file. All component-absence, full-environment,
native-advisory, physical, FIPS and deployment flags remain false. A missing
distribution entry must never automatically waive a package-level finding.

Read-only collection tests:

```sh
python3 -m unittest discover -s tests/host -p test_python_components.py -v
```


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

Each read-only archive GET permits at most three attempts for a remote disconnect,
connection reset/abort or timeout. Attempts restart the same URL and hash from an
empty owned file; discarded bytes still consume the shared byte budget. The
existing deadline covers the short retry delays. HTTP status/redirect refusals,
TLS certificate failures, digest/size mismatches, signature/parser failures,
resource limits and changed file identity remain terminal. An existing or
replaced download path is never overwritten. This transport recovery does not
clear an advisory or establish a successful hosted payload assessment.

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

Failed boots may project bounded source coordinates and fixed activation/job
categories. The host also recognizes PID 1's exact timeout sentence only for
the fixture's fixed unit identities, privately, because early device or mount
events may precede the journal. It emits only role/operation/timeout categories,
never console text, unit names or descriptions. These failure diagnostics
cannot admit startup, change deadlines or turn a failed boot into success.

The provisioned boot prepares the new filesystem through a separate private
initialization mountpoint. It must not temporarily mount/unmount the service
group's managed path while that path's start job is queued. PID 1 can treat
an externally observed active mount as job completion, then propagate its
removal to bound dependents before the provider returns. Initialization rejects
existing points, joins a mount even after a lost command result, and refuses
to qualify cleanup while its private mountpoint remains. Production mount-loss
dependencies, startup edges, deadlines and missing-key refusal remain intact.

The separate `gateway-service-boot-order.yml` experiment runs a minimal tmpfs
provider/mount/bound-consumer graph against the actual hosted Ubuntu PID 1.
Native state barriers deliberately expose the legacy interleaving and require
private initialization to preserve the pending job and subsequent startup.
It retires its own service cgroups, mounts and unit registrations before
publishing one bounded public JSON receipt. Raw native output and resource
identifiers stay private. This isolates the ordering mechanism; it does not
qualify the full gateway, encryption, cold boots, physical power loss or
deployment. Those require their separate complete evidence.

Only a complete strict public JSON receipt and its explicit public parent proofs
are uploaded. Images, keys, tokens, source archives, media and raw logs stay
private. A draft implementation or host regression pass is not evidence that
the four boots passed. This experiment does not qualify device TLS, actual VPN,
production unlock custody, power loss, physical storage, verified boot, protected
rollback, full OS/native advisory closure, FIPS or deployment.


## Signed-source extraction

The shared signed-source extractor preserves bounded archive timestamps along
with the admitted file bytes. Release-generated Autotools files must remain
newer than their inputs; extraction order cannot select an unreviewed local
Autotools regeneration. Archive paths, types, duplicates, size limits and
private permissions retain their original admission checks. A GNU Make4 host
witness verifies release ordering and confirms a genuinely changed input still
requires regeneration. Actual Expat GNU Make4 build/testing and Linux execution
remain separately qualified.

## Complete encrypted-composition receipt budget

The source/SDK/XML-bound encrypted-volume composition keeps every required
source, executable, native-runtime, key-replacement and recovery proof. Its
producer and three-case consumer share a fixed 128 KiB public JSON budget;
the previous 64 KiB budget was exhausted by the expanded evidence graph.
An over-budget case fails before file creation or JSON parsing, and aggregation
publishes only the complete third receipt byte for byte after all three distinct
volumes and their exact cohort bindings pass. This changes the host evidence
envelope, not media/storage limits or any cryptographic acceptance check.
Keys, configuration, logs, footage, disks and backup data remain excluded.

## Exact XML runtime through service cold boots

The current `gateway-service-boot.yml` fixture requires the signed Expat
qualification before constructing its private guest. The controller verifies
its own interpreter's actual loaded image and the source/image receipt before
allocating a guest root. Only the declared library and public qualification
receipt are copied into the owned root; the guest's relative SONAME link must
resolve to those exact bytes. The existing vendor image remains present.

The fixture compiles the existing vendor-header C ABI witness for execution in
the guest. On each provisioned, rebooted, locked and recovered kernel, the guest
must execute both a wrong-image refusal and the actual C parser/loader check,
then bind the current Python interpreter's namespace parse and exact native
mapping to the same parent receipt. The checker repeats current-image admission
after service cleanup. Locked-key refusal also requires these XML checks before
unlock is attempted; a failure at an earlier phase cannot qualify the locked case.

Plan, per-boot and aggregate receipts use schema 2. All four boot receipts must
retain identical XML parent/library/probe identity and executed checks. Old
schema-1 boot success and missing/mixed XML receipts refuse. The artifact adds
only the bounded public XML component JSON to the existing explicit allowlist.
The unresolved source advisory and whole-native/release/device refusals remain.
Host regressions qualify these admission/refusal contracts; actual Linux ABI and
four-kernel execution of this new cohort still require the hosted workflow.

## Signed Expat candidate for system Python

The verified Ubuntu candidate loads source-package `expat` through system Python.
The isolated `gateway-system-xml.yml` workflow qualifies the pinned Expat 2.9.0
archive and detached signature against the maintainer's separately published key.
The exact reviewed certificate is checked in at
`security/keys/expat-maintainer.asc` and included in every source receipt. Its
complete bytes, including armor headers, must match the pinned digest before
GnuPG runs. The keyserver can vary its unsigned `Version` and `Comment` headers
while returning identical certificate packets; qualification never retrieves or
normalizes a live certificate. The certificate was obtained at the maintainer's
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
CVE-2026-93990. It does not establish a fix for CVE-2025-66382, CVE-2026-113351 or
CVE-2026-115506, which are retained explicitly as unresolved. The latter two
have merged upstream fixes in [#1431](https://github.com/libexpat/libexpat/pull/1431)
and [#1433](https://github.com/libexpat/libexpat/pull/1433), after the pinned
2.9.0 release. Policy schema 2 records those exact fix commits and primary
references separately from the earlier matched distribution records. A merged
fix is not evidence that the signed release contains it; the existing source,
signature and key pins remain unchanged. A later release requires its own
review and qualification. The current upstream issue also records unresolved
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
unresolved source findings. All three reviewed unresolved advisories remain
attached to the actual Expat source version and image hash; dropping the Ubuntu
package from the executed
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

## System Python startup payload observations

`python_payloads.py` supplements the separate distribution-metadata receipt with
the actual files below the isolated system interpreter's admitted startup
directories. Run it with `/usr/bin/python3 -I` on the guarded disposable hosted
image, after `python_components.py`, using the same candidate receipt. It reads
regular files without importing them and publishes paths, sizes, modes and
SHA-256 values. It records internal symlinks only when their resolved targets
are also inventoried; escaping, dangling, changing or special-file entries
refuse collection. Schema 3 also observes explicit reviewed Ubuntu package
links: the `sitecustomize.py` conffile, Babel's global/locale data, netaddr's two
IEEE data files, the shared Python library, and LLDB's Python module directory
and its explicit native-library references. Source/target fingerprints for the
six newly observed references match public Ubuntu package definitions. Keep the
source/target pairs in the collector's reviewed tables; a version or layout
change cannot grant access to a new adjacent tree automatically.

Require protected ancestors and stable regular-file/directory descriptors.
Traverse only the two reviewed Babel/LLDB directories, retain their recursive
ownership, type, identity and resource checks, and cover duplicate/nested roots
once. Hash the individual reviewed external files without walking their parents:
for example, the startup hook never grants a walk of `/etc`. Other external
targets and all missing targets still refuse collection. An unavailable link's
bounded diagnostic has a fixed `missing` status and no claimed resolved path.
Observing installed files does not authenticate their contents. Raw link text is represented only by its digest and byte
count: an intermediate private path must not leak even when final resolution
returns to an admitted directory. Nested startup roots are covered once.

The collector requires protected root ownership, repeats the filesystem scan,
joins every observed distribution metadata/RECORD hash to the payload inventory,
and rechecks the interpreter, source and both parent receipts. Limits are
65,536 entries, 64 MiB per ordinary regular file, 1 GiB of observed payload bytes, depth
32 and a shared 120-second scan deadline. The JSON receipt is at most 16 MiB;
no payload contents, exception text or private build/runtime paths are exported.
Only the exact reviewed external LLVM library may use a 128 MiB file limit:
the public `libllvm18` archive contains a 122,969,032-byte library. The ordinary
file limit and aggregate/time limits still apply everywhere else; a missing or
different target is not admitted by that resource allowance.

This is a filesystem observation of those startup directories. Authenticating
all package contents, reviewing dynamic/frozen/extension importers and proving
component absence remain separate requirements. Every qualification, absence,
import-closure, advisory-clearance and release flag stays false. The minimized
native workflow preserves the original advisory exit status and retains both
metadata and payload receipts; collecting more evidence cannot admit a known
finding. A passing local fixture suite does not establish hosted collection.

An unreviewed external link still prevents a payload receipt. A failed walk may
identify at most 32 such links in one bounded diagnostic (at most 32 KiB).
Only source paths admitted by the public system-library path validator may be
printed; other sources, raw link text and resolved external paths are represented
only by SHA-256 and byte counts. External file contents are never read. These
diagnostics do not expand admitted targets or change any qualification gate.

A protected-path refusal retains the original fixed failure category. For an
exact source-reviewed external target only, one `PYTHON_PAYLOAD_PATH_DIAGNOSTICS`
JSON object (at most 2 KiB) may name that constant target and its first failing
lexical ancestor, with only symlink/root-owner/group-or-other-writable booleans.
Unknown or private paths and unrelated ancestors emit no path diagnostic. No
numeric owner, raw exception, file contents or relaxed protection is exported.

```sh
python3 -m unittest discover -s tests/host -p test_python_payloads.py -v
```
