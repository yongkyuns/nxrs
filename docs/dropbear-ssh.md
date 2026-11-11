# Optional Dropbear access to NSH

NXRS can select the native NuttX Apps Dropbear utility as an optional maintenance
service. SSH is default off. Applications continue to use ordinary Rust `std`
I/O, networking and threads; there is no mandatory SSH HAL, Rust protocol wrapper,
broker, launcher or executor. The implementation and qualification plan is
[issue #327](https://github.com/yongkyuns/nxrs/issues/327).

**Physical qualification remains pending.** Configuration resolution, host/native
controls and target compilation support implementation review. They do not
establish reliable device access, safe per-product memory budgets, entropy quality
or persistence. The executable
[qualification procedure](../tests/nuttx-std/ssh/README.md) records these evidence
classes separately and never converts a simulated or host result into a device
success.

## Ownership and supported scope

The SSH protocol, authentication and NSH/PTY adapter remain in the pinned
`external/nuttx-apps` dependency. NXRS owns optional configuration selection,
versioned preparation patches, fixture integration, provenance and qualification.
The authoritative patch list is
[`upstream/build-patches.json`](../upstream/build-patches.json); do not patch a
generated build tree by hand and present it as a reproducible integration.

The initial contract is a single IPv4 listener on an explicit address, TCP port
2222, one active transport and one session channel, and a password-authenticated
interactive NSH PTY. The listener is withdrawn while the active transport owns
the service and reopens only after cleanup. Readiness means `listening`, not
merely that task creation returned a PID.

| Included | Deferred or disabled |
| --- | --- |
| Interactive NSH with `ssh -tt` | SSH `exec` requests and scripts using `ssh host command` |
| Native NuttX password database | Public-key authentication and authorized-key policy |
| Unique ECDSA P-256 native Dropbear host key | Automatic replacement of an existing identity |
| PTY input/output and explicit foreground-command SIGINT | Generic forced cleanup of arbitrary uncooperative Rust commands |
| Explicit local start/status/stop | NSH autostart, direct-init startup, multiple administrative sessions |
| Bounded authentication, idle/retry and stop settings | SCP, SFTP, compression, forwarding, agent forwarding and NSH command pipelines |

The profile is a flat administrative environment. A password-protected NSH shell
does not establish user isolation, privilege separation or a filesystem sandbox.
Only trusted maintenance users should receive access. The current port does not
drop privileges. Product authorization, network exposure and physical maintenance
policy remain product decisions.

## Lifecycle

The native utility accepts:

```text
dropbear start
dropbear status
dropbear stop
```

No arguments alias foreground `start`. The foreground native invocation owns the
service until teardown. Status reports:

```text
dropbear: state=listening pid=123 result=0
```

The observable states are `stopped`, `starting`, `listening`, `session`, `stopping`
and `failed`. Stop reports success only after the daemon's session resources have
been reclaimed; its parent must still reap the native task. A stop requested by
the active SSH session/its builtin is rejected to avoid joining its own owner.
Use a separate local maintenance task or console.

With this single-session policy, a client CHANNEL_CLOSE ends the entire transport,
including cancellation of a still-live NSH session. Orderly server-initiated
session closure reports the actual shell exit status. A transport is not reused
for a second shell after its first channel closes.

`NSH_DISABLEBG=y` stays enabled, and `NSH_DROPBEAR` stays off. Do not use shell
background syntax to start a Rust-managed service. For qualification,
`ssh_probe lab` retains the existing Wi-Fi station owner and uses a thin,
test-only `exec_builtin()`/`waitpid()` translation to create/reap the foreground
native utility in a separate task. Its local input remains available for status,
snapshots and stop. Production code does not depend on this fixture.

The fixture's maintenance input/output use separately owned, unbuffered
`std::fs::File` handles. They bypass Rust's global stdin buffer and standard-I/O
locks, preserving local control while a remote Rust command blocks on its PTY.
Wi-Fi setup completes before the native child starts, and Wi-Fi teardown begins
only after all native/control children are reaped. The fixture copies its initial
arguments before launching children and permits one remote Rust command at a
time, with fully consumed bounded input and complete newline output. Its build
requires the flat image's global pthread-key/destructor support and the prepared
Rust main-thread initialization fix. This bounds qualification to the specific serialized fixture;
arbitrary concurrent/restarted Rust applications using process-wide argument or
standard-I/O state remain outside that guarantee.

PTY cancellation sends the configured interrupt character to the foreground
command. The Rust interrupt witness explicitly handles SIGINT and returns from
normal task context; global `SIG_DEFAULT` is still disabled. NSH retains its
foreground child through interrupted waits. A command that ignores cancellation
can keep ownership live beyond the stop deadline. In that case stop fails,
resources remain owned, and local recovery is required. Timeout is never proof
that tasks, descriptors, buffers or stack storage are safe to free.

## Configuration and image accounting

The fixture reuses the existing `tests/profiles/esp32s3-wifi.toml` profile and normal build frontend.
It adds only normal `--kconfig-enable`, `--kconfig-disable` and
`--kconfig-set NAME=VALUE` selections. Scalar configuration is verified against
the effective NuttX configuration; a requested value that Kconfig changes or
cannot select is a build error.

The closure includes TCP, NSH/builtins, pthreads, child/parent and wait support,
PTYs and pipes, serial termios/interrupt support, NuttX password and base64 codecs,
hardware RNG devices, and software cryptodev with its software-AES prerequisite.
`ARCH_SETJMP_H=y` selects NuttX's matching Xtensa jump-buffer header and
`setjmp`/`longjmp` implementation for native session cleanup.
A backport of [ESP HAL PR #13](https://github.com/espressif/esp-hal-3rdparty/pull/13)
prefixes the Wi-Fi AES helpers on NuttX so they can coexist with software
cryptodev's AES implementation. The pinned dependency revision is retained.
The native service is admitted only for a flat, non-SMP image. Kernel pipes remain
enabled for PTYs and session signaling, while `NSH_PIPELINE=n` disables shell
pipeline syntax alongside `NSH_DISABLEBG=y`.
SCP, compression, host-key generation and NSH Dropbear autostart are excluded.
The exact prepared algorithm header and effective configuration are retained
beside the image. Negotiated algorithm names are recorded only by real OpenSSH
device trials; source configuration is not negotiation evidence.

The enabled build verifier also checks the configured upstream revision, all
eight nested patch files, their declared order in Make and CMake, and all 16 affected
prepared source/header bytes against
[`dropbear-source-receipt.json`](../upstream/nuttx-apps/dropbear-source-receipt.json).
The host controls apply the complete sequence to checksum-verified upstream
sources. A historical zero-context patch that placed a cancellation hook inside
a comment is a negative control: the prepared-source check must reject it even
though the hook text exists and the image can link. The build retains
`ssh-dropbear-source-receipt.json`, whose hash is included in the image evidence.
These source checks establish which reviewed patch output is present; runtime
behavior still requires the stated device trials. The cross-build evidence also
records artifact byte sizes beside their hashes, so image costs stay associated
with the exact measured revision.

The SSH qualification verifier requires `FDCHECK=n` and checks
`NFILE_DESCRIPTORS_PER_BLOCK <= max(LIBC_OPEN_MAX, 16)` in the effective
configuration. At the retained NuttX revision,
[`limits.h`](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/include/limits.h)
defines that maximum as `OPEN_MAX`, and
[`select.h`](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/include/sys/select.h)
uses it as `FD_SETSIZE`.
The [descriptor allocator](https://github.com/apache/nuttx/blob/2b5c0de5e933281c209f954a63b99cf10c132346/fs/inode/fs_files.c)
bounds later table extensions by `OPEN_MAX`; this additional check bounds the
initial row as well. Encoded descriptors under `FDCHECK=y` require separate SSH
qualification. The verified values are retained in the image evidence.

The qualification recipe selects these explicit limits:

| Setting | Value |
| --- | --- |
| Listener | Explicit IPv4, port 2222; build-only default bind is loopback |
| Authentication deadline | 30 seconds |
| Authentication failures | 3 maximum attempts |
| Session idle deadline | 60 seconds |
| Deferred replies during rekey | 2 entries; the third is refused before allocation |
| Listen setup | 2 retries, maximum retry interval 2 seconds |
| Stop deadline | 10 seconds |
| Native server stack | 65,536 bytes |
| NSH session stack | 8,192 bytes |
| Qualification Rust command stack | 65,536 bytes, present in both matched images |

These are configured values, not measured minima or a total RAM estimate. Include
the native waiter, Rust/NSH child stacks, C/kernel heaps, allocator metadata, TCP
and packet buffers, PTYs, file descriptors and crypto allocations in accounting.
The Rust `GlobalAlloc` view alone is insufficient.

`build.sh --without-ssh` builds the same fixture and shared instrumentation without
the exclusive SSH/password/cryptodev selection. `check-build.py` rejects enabled
exclusive dependencies and retained server/password implementation symbols in
that image. This proves the scope of the effective-configuration/final-symbol
check. A clean build/cache audit must additionally demonstrate that an SSH-off
build did not fetch or compile the nested Dropbear dependency; a warm workspace
containing another build's source cannot establish that by directory existence.

## Upstream corrections at the retained pins

The integration retains Apps `3c772ea35b9245972e18999828f7b533e6fc57cc` and
Dropbear `54ef47adf8c99b422be6a8f694f2866e62f88b9e`. The complete nested series
includes three subsequent upstream corrections:

| Backport | Behavior and validation scope |
| --- | --- |
| [Deferred reply bound](https://github.com/mkj/dropbear/commit/d35cf53782040ab11ea38369244cdbe92fc85b9a) | Bounds replies queued during server-initiated rekey. The native threshold is one, retaining upstream's strict `>` check, so at most two entries are admitted. Initialization and draining reset the counter. |
| [ECDSA identifier comparison](https://github.com/mkj/dropbear/commit/cfedf9889222e44b2a31ecc14a289b7d0d86c563) | Replaces a fixed-length comparison with a terminated-string comparison. A direct helper witness reproduces the historical short-identifier over-read; the review did not establish that the ordinary native key-loader dispatcher admits that input. |
| [Channel packet bounds](https://github.com/mkj/dropbear/commit/59870ad43153fe8d4f1c96f5d5752116c94f31ff) | Clamps channel-open and confirmation packet limits to `[1, TRANS_MAX_PAYLOAD_LEN]`, preventing a peer's zero limit from stalling progress. |

The one-channel admission limit does not itself bound rejection replies during
rekey. Native sanitizer controls compile the actual prepared functions and
demonstrate the historical queue, parser and packet-limit failures when each
corresponding backport is omitted. Server-initiated rekey with repeated rejected
requests still needs a device trial; the harness's client-triggered rekey case
does not exercise that condition.

The effective-config verifier also requires `SIG_DEFAULT=n`, which excludes the
default SIGPIPE termination condition addressed by the later
[Apps SIGPIPE change](https://github.com/apache/nuttx-apps/commit/b75aab77d18e2eb5fc371351df844f6c0d3306a2).
The qualified build path uses Make. Both recipes apply the same reviewed patch
sequence with fatal failures, but CMake build qualification remains pending,
including review of the later [Apps CMake corrections](https://github.com/apache/nuttx-apps/commit/7691b5a6ae40d3e084b542a54ad813cb78155058).

## Entropy, crypto and provisioning

The native SSH admission path requires an admitted hardware entropy source and
successful nonblocking `/dev/random` reads. It must refuse unavailable/unsupported
entropy instead of substituting a clock seed or a weak PRNG. Reuse the existing
board entropy qualification in [issue #35](https://github.com/yongkyuns/nxrs/issues/35).
The ESP32-S3 lab procedure starts the Wi-Fi station before provisioning and SSH
startup, preserving the required radio lifetime. Hardware source/fault behavior
still needs device evidence.

The port uses `/dev/crypto` for configured operations, including HMAC-SHA256 and
ChaCha20/Poly1305. A selected software-crypto Kconfig symbol is not sufficient
proof that the device exists, initializes and supports the required requests.
Exercise startup and handshake with the real target device, plus unavailable
entropy/crypto controls, before admitting the product configuration.

Host keys use native Dropbear ECDSA P-256 format. Generate a unique key for each
device through a trusted process and record its public fingerprint independently.
`NETUTILS_DROPBEAR_GENERATE_HOSTKEY=n` selects provisioned identity. Missing,
unreadable or invalid identity must fail startup; an existing read-only key must
not cause generation or replacement.

The credential-safe lab procedure transfers the key and account secret through
a length-bounded, no-echo local-console frame. It creates configured parent
directories and calls native `passwd_adduser()`; password records use the pinned
NuttX PBKDF2-HMAC-SHA256 modular format. Merely initializing password support does
not create an account. The fixture refuses to provision over an existing key or
password database. It is for trusted, isolated bench use with the daemon stopped,
and is not a manufacturing transaction or a power-loss recovery design.

Use an actual mounted persistent filesystem for deployed identity and database
paths. Check permissions, storage errors, read-only operation and stability across
reboot. The optional TMPFS bring-up path is explicitly disposable: a successful
write or fingerprint match there does not prove persistence. If future products
enable on-device key generation, separately qualify entropy, writable persistent
storage, incomplete writes and restart behavior.

Do not pass SSH/Wi-Fi passwords in shell arguments or environment variables, bake
keys/accounts into firmware, or save raw secret-bearing console transcripts.
The harness reads private owned mode-0600 files outside the repository or uses a
terminal prompt. Its OpenSSH child gets the password only through a verified
no-echo controlling terminal. Public fingerprints and host-key records may be
retained as evidence.

## Qualification still required on hardware

The automated fixture is a starting point for this matrix. Preserve failures and
raw **public measurement** data, and record image/config/source/compiler hashes,
hardware identity, test commands and acceptance budgets before declaring a pass.

| Area | Required device evidence |
| --- | --- |
| Authentication storage | Correct/wrong password, unknown user, empty/unprovisioned, corrupt and unreadable database; maximum-attempt and authentication deadlines |
| Identity | Correct native format; missing/invalid/unreadable key refusal; stable public fingerprint across restart/reboot and read-only mounts; no silent replacement |
| Terminal/application behavior | NSH, partial input, resize, Ctrl-C, Rust stdin/stdout/stderr, ordinary threads, success/failure return, return to the owning shell and continuing local workload |
| Admission | One transport and one channel, bounded incomplete handshakes and second requests, unavailable address/address-in-use, bounded listen retries |
| Recovery | Abrupt client death, sustained output/backpressure, rekey, real network loss/recovery, idle deadline, idle and active stop, restart without reboot |
| Negative controls | Deliberately failed login, missing output, changed identity, injected task/FD leak and incomplete shutdown must be detected; host controls do not replace device injection |
| Resource cost | Matched baseline, dependencies selected but not running, listening, authenticated shell, handshake/rekey/failure peaks, post-disconnect and post-stop |
| Long runs | At least 100 reconnects after warm-up, including failed logins and active stop/restart in one boot; no hidden reset between samples |
| Coexistence | Independent firmware progress during authentication/output/rekey plus product-workload latency, missed deadlines, buffer pressure and recovery |

Use `/proc/meminfo`, task status/stack coloration, descriptor/TCP/PTY observations
and linker maps. Separate flash, IRAM, DRAM and PSRAM placement. Record configured
and observed stack usage, total C/kernel heap behavior and peak allocations.
Set board/product-specific budgets before interpreting measurements; no general
“SSH uses X KiB” claim follows from the configured server stack.

The harness saves a new trial directory with machine-readable results, verified
public identity, negotiated algorithms and explicit procfs measurements. Its
task/FD stability check compares the same state after warm-up; shared-group FD
rows are not a deduplicated system count. Missing observations fail their check.
The overall result retains pending gates even when the scripted cases pass.
This distinction keeps software readiness separate from completing issue #327.
