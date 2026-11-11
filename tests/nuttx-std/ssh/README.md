# Dropbear / NSH qualification

This fixture exercises the optional native NuttX Dropbear server with a real
OpenSSH client. It is tracked in [issue #327](https://github.com/yongkyuns/nxrs/issues/327).
The [integration reference](../../../docs/dropbear-ssh.md) describes ownership,
configuration, security boundaries and the remaining qualification gates.

**Hardware qualification is pending.** Host unit tests, compiled native controls
and a successful target cross-build are separate evidence classes. This directory
does not contain a captured physical SSH success or a measured memory budget.

## Build the matched images

Use the existing `tests/profiles/esp32s3-wifi.toml` qualification profile and the normal build
frontend. No credentials, private keys or physical device are needed to build.

```sh
bash tests/nuttx-std/ssh/build.sh --dry-run
bash tests/nuttx-std/ssh/build.sh --without-ssh --out target/dropbear-ssh-off
bash tests/nuttx-std/ssh/build.sh --out target/dropbear-ssh \
  --bind 192.168.50.42
```

Reserve the chosen device address in the lab DHCP server. The default bind is
`127.0.0.1`, suitable for build-only CI and inaccessible to an external SSH client.
There is no wildcard listener in this recipe. The port is 2222. The SSH-off image
contains the same Rust fixture and shared instrumentation, with no enabled
Dropbear or password/cryptodev implementation. Its test-only `start` operation
returns unavailable because no native `dropbear` builtin is registered.

`build.sh` selects the pinned dependency's networking, PTY, child/wait, password,
codec, hardware entropy and software cryptodev prerequisites. It verifies the
effective configuration and final symbols through `check-build.py`. The enabled
image also retains the exact prepared `ssh-localoptions.h`. Both images require
the prepared Rust main-thread-initialization fix and its hashed receipt. The resulting
`ssh-build-evidence.json` is **crossbuild** evidence. It does not prove successful
authentication, terminal behavior, entropy quality or physical board operation.

For an actual mounted persistent filesystem, select its paths explicitly:

```sh
bash tests/nuttx-std/ssh/build.sh --out target/dropbear-ssh \
  --bind 192.168.50.42 \
  --hostkey-path /mnt/flash/ssh/dropbear_ecdsa_host_key \
  --passwd-path /mnt/flash/ssh/passwd
```

Additional native build options can follow `--`; use them for the board's
existing storage/controller configuration when needed. Merely naming a directory
does not mount storage or make it persistent. The default `/mnt/ssh` recipe below
uses explicitly requested TMPFS for disposable bring-up and cannot establish
identity persistence across reboot.

Flash the generated image using the board's normal flashing procedure. The
existing Wi-Fi fixture documents the ESP32-S3 merged-image layout. Record the
image hash actually flashed, board revision, flash/PSRAM population, serial
device, access point and toolchain/build provenance with the experiment.

## Prepare disposable credentials without putting secrets in arguments

The host needs Python 3, OpenSSH `ssh` and `ssh-keygen`, `pyserial` for the
maintenance console, and the host `dropbearkey` tool for **native Dropbear**
private-key format. An OpenSSH PEM/private-key file is not an interchangeable
input to this fixture.

Create a unique ECDSA P-256 identity for this device. Keep the private files in a
mode-0700 directory outside the repository and build/artifact trees:

```sh
umask 077
SSH_TEST_SECRETS="$(mktemp -d /tmp/nxrs-ssh-secrets.XXXXXX)"
dropbearkey -t ecdsa -s 256 -f "$SSH_TEST_SECRETS/dropbear_ecdsa_host_key"
dropbearkey -y -f "$SSH_TEST_SECRETS/dropbear_ecdsa_host_key" \
  > "$SSH_TEST_SECRETS/public.txt"
python3 - "$SSH_TEST_SECRETS" <<'PY'
import getpass
from pathlib import Path
import sys
directory = Path(sys.argv[1])
for filename, prompt in [('ssh-password', 'Disposable SSH password: '),
                         ('wifi-password', 'Lab WPA2 passphrase: ')]:
    value = getpass.getpass(prompt)
    (directory / filename).write_text(value + '\n')
    (directory / filename).chmod(0o600)
public = [line.split() for line in (directory / 'public.txt').read_text().splitlines()
          if line.startswith('ecdsa-sha2-nistp256 ')]
assert len(public) == 1
(directory / 'known_hosts').write_text(
    f'[192.168.50.42]:2222 {public[0][0]} {public[0][1]}\n')
PY
ssh-keygen -l -E sha256 -f "$SSH_TEST_SECRETS/known_hosts"
```

The NuttX password utility requires 8–256 characters with uppercase, lowercase,
a digit and a supported special character, such as `!`. This fixture admits
printable ASCII. Use a new disposable credential, not a personal account
password. The Wi-Fi fixture accepts an 8–63 byte printable ASCII WPA2 passphrase.

Record the displayed `SHA256:…` fingerprint through this trusted provisioning
path. The harness does not establish trust through `ssh-keyscan` or disable
host-key verification. It validates the supplied fingerprint, creates an isolated
public `known_hosts` file for the trial, and uses `StrictHostKeyChecking=yes`.

## Run the physical experiment

Disconnect other serial-console clients first. The harness does not reset the
board when opening the console. Run from the repository root, replacing the
serial path, SSID, address and fingerprint:

```sh
python3 tests/nuttx-std/ssh/run.py \
  --serial-port /dev/ttyACM0 \
  --ssid nxrs-lab \
  --wifi-password-file "$SSH_TEST_SECRETS/wifi-password" \
  --host 192.168.50.42 --port 2222 --user nxrs_test \
  --password-file "$SSH_TEST_SECRETS/ssh-password" \
  --provision-key "$SSH_TEST_SECRETS/dropbear_ecdsa_host_key" \
  --known-hosts "$SSH_TEST_SECRETS/known_hosts" \
  --expected-fingerprint 'SHA256:REPLACE_WITH_VERIFIED_FINGERPRINT' \
  --build-evidence target/dropbear-ssh/ssh-build-evidence.json \
  --tmpfs --reconnects 100 --out target/ssh-qualification
```

`--tmpfs` explicitly requests a new disposable mount at `/mnt/ssh`. For provisioned
persistent storage, mount it through the board's normal local procedure, use the
matching build paths, and omit `--tmpfs`. On subsequent runs omit
`--provision-key`; existing identity and password files are preserved. Provisioning
refuses if either configured path already exists. It creates parent directories,
receives the key/password only after disabling console echo, and uses the actual
NuttX `passwd_adduser()` implementation to produce the password database. It does
not issue NSH `useradd user password`, which would expose a password in arguments
and console/history. A failed new provision attempts to remove its partial files
and reports cleanup failures. This is a lab provisioner, not a power-fail-safe
manufacturing process.

The local `ssh_probe lab` command reuses the existing Wi-Fi credential protocol,
station/DHCP owner, explicit disconnect and worker join. It retains the addressed
Wi-Fi connection throughout the SSH experiment. A mechanical test-only
`exec_builtin()` bridge starts the native foreground server in its own task;
Rust retains and reaps that child. `NSH_DISABLEBG=y` remains enforced. The recipe
does not use `dropbear &`, a new application runtime, or the Wi-Fi echo test that
disconnects before returning.

The local owner duplicates its stdin/stdout/stderr into owned, unbuffered
`std::fs::File` handles. Its command input, provisioning frames and output do not
take Rust's process-wide standard-I/O buffers or locks. Remote probes deliberately
use ordinary `std::io`, so a blocked remote read/write cannot also block local
stop/progress through the same Rust lock. The Wi-Fi helper's global-I/O setup
runs before the daemon is created, and its teardown runs after the daemon and
control children have been reaped.

This fixture copies its arguments before creating remote children, serializes
remote Rust commands, fully consumes bounded input lines and writes complete
newline-terminated output. The flat runtime's image-wide TLS-key/destructor
configuration and one-time main-thread initialization are required. General
concurrent or restarted Rust applications with shared argument/standard-I/O
state still require separate qualification; the procedure covers only the stated
fixture conditions. The recipe enables kernel pipes for PTYs/session
signaling and explicitly disables NSH command-pipeline syntax.

The local lab console accepts `start`, `status`, `stop`, `snapshot`, `progress`,
`provision` and `quit`. Do not run this owner through an SSH session. It is the
independent maintenance path for stopping SSH and observing failure. A failed
stop retains network/native ownership for recovery; it does not report successful
teardown or release the network underneath a live server. Uncooperative foreground
commands can cause bounded stop failure. The fixture's interrupt witness installs
an explicit SIGINT handler; global default signal actions remain disabled.

For an already provisioned and independently controlled server, omit all serial
and Wi-Fi/provisioning options. The SSH checks still execute, but local lifecycle,
authentication-deadline, same-boot ownership and resource measurements remain
unverified. A client reconnect loop alone is not leak evidence.

## What the automated cases establish

| Case | Required observation |
| --- | --- |
| Host-key negative control | A deliberately different trusted key is rejected before any password is sent. |
| Authentication | Correct disposable credential opens NSH; wrong password and unknown user are explicitly rejected. |
| Rust I/O and threads | Token-matched stdin, stdout, stderr and ordinary joined-thread markers appear on the session PTY. PTYs merge stdout/stderr; the markers verify both writes, not separate SSH streams. |
| Error return | The Rust task exits 7; the pinned interactive NSH exposes its failure as `$? = 1`. Exact child status 7 is checked by the host fixture test, not claimed for NSH `$?`. |
| Terminal | Partial command input, reported 37×113 resize and Ctrl-C to the explicit foreground Rust handler. |
| Limits | The active transport remains usable while a second connection and a second session channel over an OpenSSH master are refused. |
| Large output and rekey | 8,192 ordered lines survive a one-second client-read pause, and OpenSSH reports an additional received NEWKEYS. This alone does not quantify target backpressure or peak memory. |
| Excluded requests | Remote exec, legacy SCP exec, SFTP subsystem, remote forwarding and direct TCP forwarding are explicitly refused. |
| Reconnects | By default 100 successful sessions, interleaved wrong-password logins, within the same local-owner lifetime. No reboot masks accumulated state. |
| Local control | Native readiness/state, authentication-deadline reclamation, progress during output, task/FD snapshots, active stop of a cooperative Rust builtin, restart, final child reap and Wi-Fi join. |

Every run creates a new timestamped directory; failed trials remain present.
`summary.json` contains narrow public results, negotiated algorithms and failure
codes. `host-tools.json` records OpenSSH and the verified public fingerprint.
Resource files retain only explicit `/proc/meminfo`, task IDs, stack and descriptor
observations. Private keys, passwords and raw authentication/serial transcripts
are not saved. Secret echo detection covers adjacent read chunks; on detection
output is withheld and the case fails.

The descriptor witness counts rows per task, so shared task groups may be counted
more than once. It compares the same state before/after a campaign and treats any
task/descriptor growth as failure. It is not a unique-system-FD total or a full
heap/stack budget. Missing procfs/stack/descriptor data fails the relevant check.

Even when all automated cases pass, the summary says
`checks_passed_full_qualification_pending`. Complete the physical fault,
persistence, memory and workload-latency matrix in the integration reference
before declaring issue #327 qualified.

## Host/control tests

```sh
python3 -m unittest discover -s tests/host -p test_ssh_qualification.py -v
cargo check --locked -p nxrs-std-ssh
python3 tools/check-architecture.py
```

These tests use real local PTYs, OpenSSH configuration/key tools and the host-built
Rust probe. They validate secret handling, missing-output detection, identity
mismatch, task/FD leak controls, incomplete shutdown detection and local console
I/O while all global standard-I/O locks are held. The native
Dropbear controls are tested separately in `tests/host/test_dropbear_native.py`.
None of these is a physical NuttX SSH result. The public test inputs and temporary
host keys are disposable test material, not installed device credentials.
