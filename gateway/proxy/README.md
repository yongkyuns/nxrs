# Approved gateway TLS proxy

Generated VPN and strict services require `nxrs-gateway-proxy`. The synchronous
Rust process verifies a root-installed approval policy, nginx image and its exact
four dynamic libraries, checks the loader's actual resolution, then replaces
itself with foreground nginx. It has no Tokio or Python runtime. The qualified
profile is Linux/amd64 with glibc, nginx 1.30.6 and static PCRE2 10.49. Other
operating systems/architectures refuse; this is gateway service wiring, separate
from the shared embedded-device TLS implementation.

Generated services require `--selection`, `--config` and `--runtime` as absolute
canonical paths. The bounded regular `active.json` selects exactly one
`versions/<approval-sha256>/approval.json` and its sibling `nginx`. The policy's
actual bytes must match that digest before ordinary admission. No executable,
library directory or extra command arguments are selected from JSON. Missing or
invalid selection never falls back to an unversioned bundle or stock nginx.
`--check` tests nginx configuration after the same admission. Explicit `--policy`
remains available for flat-bundle inspection; it cannot be combined with
`--selection`, and generated production services do not choose that mode. Duplicate/unknown fields refuse. The
compiled source policy fixes the publisher advisory review and its maximum
expiry; the administrator can shorten that interval. Expiry is checked at each
startup, not continuously during an already-running session. Clock integrity is
a prerequisite. Keep the expiring review renewed and requalify before restart.

Artifact files and every ancestor must be root-owned and not group/other
writable; symlinks, hard-linked files, set-ID files and files above their size
budget refuse. File descriptors use `O_NOFOLLOW`, bounded reads and metadata
checks around hashing. The fixed hashed GNU loader receives a cleared environment,
disabled cache and hardware-capability subdirectories, and the one reviewed
library directory. Its bounded three-second `--list` result must name exactly the
hashed loader, libc, libssl and libcrypto. The same invocation executes nginx.
`/etc/ld.so.preload` must be absent. OpenSSL uses `/dev/null` configuration and an
absent module directory, so this profile admits no configured external providers.
The generated runtime is private mode 0700 and belongs to the service UID.

These checks depend on the integrity of root, systemd's launch environment, the
launcher itself, kernel and filesystem. A root administrator can change policy
or executable paths. This is local artifact admission, not a signed release
system, rollback prevention or automatic authorization of CI output. Library
updates deliberately prevent startup until the updated graph is qualified and
approved. Do not make the library pins optional to work around that refusal.
The actual selected graph, including the launcher, remains in native inventory.

## Prepare, review and install

The source/signature builder is restricted to disposable GitHub-hosted Ubuntu
24.04 runners. From its same clean checkout, as the unprivileged build user:

```sh
cargo build --locked --manifest-path gateway/Cargo.toml -p nxrs-gateway-proxy
python3 gateway/tools/stage_proxy.py --output /absolute/new/private-review
```

Preparation revalidates the actual source/receipt/binary/library joins and copies
only nginx, its bounded public qualification receipt and `review.json` into a new
private directory. Existing destinations refuse. Partial failures retain an
unusable directory for inspection; they do not overwrite a prior approval.
Preparation prints the review, binary and receipt digests. It never installs,
starts services or grants release approval. The JSON receipt is unsigned evidence.

An administrator reviews the exact checkout, compiler/build environment, source
signatures, advisory scope, generated configuration and test evidence, and
confirms the bundle and launcher digests through the release approval process.
Full release bootstrap/signature-key revocation and remaining native findings
are still open; a hosted pass does not satisfy those gates. This change does not
authorize deploying the candidate.

After deployment is separately approved, install the reviewed launcher at
`/usr/local/libexec/nxrs-gateway-proxy` (root,0755) and the companion
`nxrs-gateway-proxy-install` utility. Create the root-owned0755 destination
`/usr/local/lib/nxrs-proxy`; every ancestor must satisfy the existing protected
directory checks. Stop and join the intended service group using the strict
recovery procedure before a production change. The installer never starts,
stops or restarts services, changes network/trust settings, or resets denial state.

Supply the review and qualification digests from the independent release
approval record, not values automatically authorized because CI produced them:

```sh
nxrs-gateway-proxy-install --review-dir /absolute/approved/private-review \
  --review-sha256 APPROVED_REVIEW_SHA256 \
  --qualification-sha256 APPROVED_QUALIFICATION_SHA256 \
  --destination /usr/local/lib/nxrs-proxy --expected-active none
```

Use `none` only for first activation; a replacement supplies the currently
selected approval digest. A stale request refuses without changing the selector.
Repeating the already selected reviewed pair confirms directory durability
without rewriting it. Approval is still an external release decision: explicit
hash arguments and unsigned qualification JSON are not a signing-key lifecycle.

The installer accepts only the three fixed source files, bounded and hash-checked
through opened descriptors. It stages a complete new version, checks the actual
nginx/loader/library graph, flushes files and directory dependencies, and atomically
renames one selector file. Existing version identities are never overwritten or
repaired in place. Versioned `nginx` and `approval.json` are0755/0644; the retained
qualification document is root-private0600. Previous versions remain available to
readers that already selected them. No symlink alias is used. TLS credentials
continue through generated `LoadCredential` entries.

Writers use one persistent private OS lock; never unlink it or bypass it with a
second deployment script. An interrupted stage does not become active. Retry can
use a fresh stage or validate and resync an existing complete version. At most32
inactive stage directories and256 completed versions are admitted; exhausted or
corrupted stores refuse and require explicit operator inspection. Nothing prunes
previous versions automatically. Do not retire one until every process that can
reference it has joined.

If selector replacement succeeds but its parent sync fails, the utility reports
`selection-visible-durability-uncertain`. The new complete pair may already be
visible; do not assume the old one remains selected or blindly roll it back.
Inspect the selector and retry the same independently approved pair to validate
dependencies and confirm durability. These filesystem barriers and process-crash
tests are not physical power-loss qualification or protected rollback prevention.

After successful installation, explicitly start the generated unit. Both pre-start configuration testing and the
foreground process use identical admission. Missing/changed/expired artifacts
refuse without falling back to `/usr/sbin/nginx`. An actual strict-service failure triggers a bounded root systemd handler to
queue a stop of only the three named application units and their target, including
when target activation failed; restore the reviewed bundle and explicitly restart.
No startup path reseeds revocations or modifies an approved artifact.

## Qualification limits

The hosted systemd fixture copies an explicitly reviewed *fixture* bundle to a
new root-owned `/run` tree. It keeps the generated sandbox and loaded TLS
credentials, actual HTTPS/WHEP, selective revocation, abrupt-controller fail-stop,
explicit recovery and missing/corrupt state checks. Eight negative cases cover
missing, altered, symlinked and writable images, altered library pin, expired,
missing and duplicate policy, followed by restored-bundle HTTPS recovery. Only a
bounded public service receipt, its exact review policy and its own source-build
qualification receipt are retained together, after owned services/accounts/files
join and cleanup succeeds. Their hashes and complete source/library vectors must
join; a different workflow's binary cannot stand in for this service's cohort. No private keys, URLs, media or native logs are uploaded.

The separate `gateway-proxy-pair.yml` fixture uses the actual Rust installer and
launcher on a disposable hosted Linux/amd64 runner. A second image appends a
fixed nonloaded ELF trailer to the exact source-qualified nginx; its fixture
receipt explicitly excludes publisher-signature coverage of that trailer. Both
selected pairs execute the shared four-category HTTPS corpus through the real
launcher, and the transformed image separately executes it before approval.

The debugger stops only its owned installer at fsync entry: an incomplete stage,
a complete pending selector, and the parent-directory flush after replacement.
It also substitutes EIO at the return from that last owned syscall, after the
real flush completes, and requires the actual utility's durability-uncertain
error. This exercises error handling without claiming a failed storage device. The stopped writer retains its real lock while a
second real installer must refuse. Each interruption joins its debugger and
tracee, checks the visible complete pair, retries, and confirms idempotence.
Sixteen selections run alongside actual launcher configuration checks. Changed
versions and malformed, missing, linked or writable selectors must refuse.

The debugger runs in a named transient systemd service with a runtime limit and
control-group termination. Cleanup verifies no unit job, process or nested
cgroup member remains before deleting its owned tree. Only five bounded public
JSON documents are retained: one result and both review/qualification pairs.
Keys, media, source archives, disks, debugger output and native logs stay local.
These are process-interruption tests; they do not qualify physical power loss,
protected rollback or an external release-approval/signing lifecycle. See the
[GDB syscall catchpoint documentation](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Set-Catchpoints.html)
and [systemd-run lifecycle documentation](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html).

Host/CI checks do not establish physical-device, NuttX, FIPS, complete native
advisory or deployment qualification. The earlier minimized graph retained 52
package/advisory matches; adding a launcher requires a fresh graph assessment.

Strict cleanup is latched with `RemainAfterExit=yes`. Recovery first joins all
application/main/pre-start/control/cgroup jobs, then stops the completed latch,
resets failed state and starts the target. Repeated failure notifications cannot
queue another cleanup action while the latch is active. Follow the strict guide
when rearming the group; the hosted fixtures require the same barriers.
