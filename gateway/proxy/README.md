# Approved gateway TLS proxy

Generated VPN and strict services require `nxrs-gateway-proxy`. The synchronous
Rust process verifies a root-installed approval policy, nginx image and its exact
four dynamic libraries, checks the loader's actual resolution, then replaces
itself with foreground nginx. It has no Tokio or Python runtime. The qualified
profile is Linux/amd64 with glibc, nginx 1.30.6 and static PCRE2 10.49. Other
operating systems/architectures refuse; this is gateway service wiring, separate
from the shared embedded-device TLS implementation.

`--policy`, `--config` and `--runtime` are required absolute canonical paths;
`--check` runs nginx's configuration test after the same admission. The policy
names no executable, library directory or extra command arguments. Its nginx is
the regular `nginx` file beside the policy. Duplicate/unknown fields refuse. The
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

After deployment is separately approved, the administrator installs the reviewed
launcher at `/usr/local/libexec/nxrs-gateway-proxy` (root, mode 0755) and the
reviewed bundle into a new root-owned `/usr/local/lib/nxrs-proxy` (mode 0755):
`nginx` (0755) and the reviewed `review.json` as `approval.json` (0644). All
ancestors must meet the same root ownership/write restrictions. Keep the receipt
and independent approval record with the release evidence. Do not put this public
approval policy under the private credential directory or use a symlink alias.
Configure the TLS credentials through the generated `LoadCredential` entries.

Install and switch only while the service group is stopped, then explicitly
start it under the generated unit. Both pre-start configuration testing and the
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

Host/CI checks do not establish physical-device, NuttX, FIPS, complete native
advisory or deployment qualification. The earlier minimized graph retained 52
package/advisory matches; adding a launcher requires a fresh graph assessment.
