#!/usr/bin/env python3
"""Real OpenSSH/PTY qualification. Host unit tests never become device evidence."""
import argparse
import errno
import fcntl
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pty
import re
import secrets
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import time

ROOT = Path(__file__).resolve().parents[3]
PROMPT = rb"(?:^|[\r\n])nsh> ?"


class CheckFailure(RuntimeError):
    """Messages are fixed diagnostic codes, never raw peer/console output."""


def secret_file(path, maximum=256):
    """An owned private regular file outside the repository, with no symlink."""
    path = Path(path).absolute()
    resolved = path.resolve(strict=True)
    if resolved == ROOT or ROOT in resolved.parents:
        raise CheckFailure("secret_file_inside_repository")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CheckFailure("secret_file_must_be_owned_regular_mode_0600")
        value = os.read(descriptor, maximum + 2)
        if len(value) > maximum + 1:
            raise CheckFailure("secret_file_too_large")
        return value
    finally:
        os.close(descriptor)


def password_file(path):
    value = secret_file(path).removesuffix(b"\n")
    if not 8 <= len(value) <= 256 or not all(32 <= byte <= 126 for byte in value):
        raise CheckFailure("password_must_be_8_to_256_printable_ascii_bytes")
    return value


class SecretGuard:
    """Detect an echoed secret even when it straddles transport read chunks."""
    def __init__(self, values):
        self.values = tuple(value for value in values if value)
        self.tail = b""
        self.keep = max((len(value) for value in self.values), default=1) - 1

    def feed(self, data):
        combined = self.tail + data
        if any(value in combined for value in self.values):
            raise CheckFailure("secret_echo_detected_output_withheld")
        self.tail = combined[-self.keep:] if self.keep else b""


class PtyProcess:
    """A controlling terminal, so OpenSSH can read its password from /dev/tty."""
    def __init__(self, argv, sensitive=()):
        self.guard = SecretGuard(sensitive)
        self.pending = bytearray()
        self.newkeys = 0
        self._debug_tail = b""
        self._debug_line = bytearray()
        self.algorithms = {}
        self.returncode = None
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            env = dict(os.environ, LC_ALL="C", TERM="xterm", SSH_ASKPASS_REQUIRE="never")
            try:
                os.execvpe(argv[0], argv, env)
            except OSError:
                os._exit(127)
        os.set_blocking(self.fd, False)

    def read(self, timeout):
        if not select.select([self.fd], [], [], max(0, timeout))[0]:
            self.poll()
            return False
        try:
            value = os.read(self.fd, 65536)
        except OSError as error:
            if error.errno == errno.EIO:
                value = b""
            else:
                raise
        if not value:
            self.poll()
            return False
        self.guard.feed(value)
        debug = self._debug_tail + value
        marker = b"SSH2_MSG_NEWKEYS received"
        self.newkeys += debug.count(marker)
        self._debug_tail = debug[-(len(marker) - 1):]
        self._debug_line.extend(value)
        while b"\n" in self._debug_line:
            line, _, rest = self._debug_line.partition(b"\n")
            self._debug_line = bytearray(rest)
            for label, pattern in (
                ("key_exchange", rb"debug1: kex: algorithm: ([A-Za-z0-9@._+-]+)"),
                ("host_key", rb"debug1: kex: host key algorithm: ([A-Za-z0-9@._+-]+)"),
                ("server_to_client", rb"debug1: kex: server->client cipher: ([A-Za-z0-9@._+-]+) MAC: ([A-Za-z0-9@._+<>-]+) compression: ([A-Za-z0-9@._+-]+)"),
                ("client_to_server", rb"debug1: kex: client->server cipher: ([A-Za-z0-9@._+-]+) MAC: ([A-Za-z0-9@._+<>-]+) compression: ([A-Za-z0-9@._+-]+)"),
            ):
                match = re.search(pattern, line)
                if match:
                    self.algorithms[label] = [group.decode("ascii") for group in match.groups()]
        if len(self._debug_line) > 16384:
            self._debug_line = self._debug_line[-4096:]
        self.pending.extend(value)
        if len(self.pending) > 2 * 1024 * 1024:
            raise CheckFailure("bounded_client_output_exceeded")
        return True

    def poll(self):
        if self.returncode is None:
            child, status = os.waitpid(self.pid, os.WNOHANG)
            if child:
                self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def expect(self, pattern, timeout=15):
        compiled = re.compile(pattern, re.M)
        deadline = time.monotonic() + timeout
        while True:
            match = compiled.search(self.pending)
            if match:
                consumed = bytes(self.pending[:match.end()])
                groups = match.groups()
                del self.pending[:match.end()]
                return consumed, groups
            if self.poll() is not None:
                # Drain any last PTY bytes before classifying a missing marker.
                if self.read(0):
                    continue
                raise CheckFailure("child_exited_before_expected_marker")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CheckFailure("expected_marker_timeout")
            self.read(min(remaining, 0.2))

    def send(self, value):
        deadline = time.monotonic() + 5
        while value:
            try:
                count = os.write(self.fd, value)
                value = value[count:]
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CheckFailure("pty_write_timeout")
                select.select([], [self.fd], [], 0.1)

    def wait(self, timeout=15):
        deadline = time.monotonic() + timeout
        while self.poll() is None:
            if time.monotonic() >= deadline:
                raise CheckFailure("child_exit_timeout")
            self.read(0.1)
        while self.read(0):
            pass
        return self.returncode

    def resize(self, rows, columns):
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        os.kill(self.pid, signal.SIGWINCH)

    def close(self, abrupt=False):
        try:
            if self.returncode is None:
                try:
                    os.killpg(self.pid, signal.SIGKILL if abrupt else signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    self.wait(1)
                except CheckFailure:
                    # A secret-echo failure must still reap the child and close
                    # the terminal. Do not read any more untrusted output here.
                    try:
                        os.killpg(self.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    if self.returncode is None:
                        _, status = os.waitpid(self.pid, 0)
                        self.returncode = os.waitstatus_to_exitcode(status)
        finally:
            os.close(self.fd)


def verify_known_hosts(path, host, port, expected):
    path = Path(path).resolve(strict=True)
    if any(character.isspace() for character in str(path)):
        raise CheckFailure("known_hosts_path_must_not_contain_whitespace")
    lookup = f"[{host}]:{port}" if port != 22 else host
    found = subprocess.run(["ssh-keygen", "-F", lookup, "-f", str(path)],
                           capture_output=True, check=False)
    records = b"\n".join(line for line in found.stdout.splitlines() if line and not line.startswith(b"#"))
    if found.returncode or not records:
        raise CheckFailure("trusted_target_host_key_missing")
    with tempfile.NamedTemporaryFile() as public:
        public.write(records + b"\n")
        public.flush()
        result = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", public.name],
                                capture_output=True, check=False)
    fingerprints = [line.split()[1].decode("ascii") for line in result.stdout.splitlines() if len(line.split()) >= 2]
    if result.returncode or fingerprints != [expected]:
        raise CheckFailure("trusted_host_fingerprint_mismatch_or_multiple_keys")
    return path


class OpenSsh:
    def __init__(self, host, port, user, known_hosts, password):
        self.host, self.port, self.user = host, port, user
        self.known_hosts, self.password = Path(known_hosts), password

    def arguments(self, *, tty=True, user=None, known_hosts=None, control=None,
                  master=False, command=(), extra=()):
        options = {
            "UserKnownHostsFile": str(known_hosts or self.known_hosts),
            "GlobalKnownHostsFile": "/dev/null", "StrictHostKeyChecking": "yes",
            "UpdateHostKeys": "no", "VerifyHostKeyDNS": "no", "CheckHostIP": "yes",
            "HostKeyAlgorithms": "ecdsa-sha2-nistp256",
            "PubkeyAuthentication": "no", "KbdInteractiveAuthentication": "no",
            "PasswordAuthentication": "yes", "PreferredAuthentications": "password",
            "NumberOfPasswordPrompts": "1", "IdentityAgent": "none",
            "IdentitiesOnly": "yes", "BatchMode": "no", "ConnectionAttempts": "1",
            "ConnectTimeout": "10", "ServerAliveInterval": "5", "ServerAliveCountMax": "2",
            "ForwardAgent": "no", "ForwardX11": "no", "Compression": "no",
            "ControlMaster": "yes" if master else "no", "ControlPersist": "no",
            "ControlPath": str(control) if control else "none",
            "RekeyLimit": "16K", "LogLevel": "DEBUG1",
        }
        result = ["ssh", "-4", "-F", "/dev/null", "-p", str(self.port), "-tt" if tty else "-T"]
        for name, value in options.items():
            result.extend(["-o", f"{name}={value}"])
        result.extend(extra)
        result.append(f"{user or self.user}@{self.host}")
        result.extend(command)
        return result

    def process(self, **kwargs):
        return PtyProcess(self.arguments(**kwargs), sensitive=(self.password,))

    def password_prompt(self, process, password=None):
        process.expect(rb"(?i)password: ?$", 20)
        if termios.tcgetattr(process.fd)[3] & termios.ECHO:
            raise CheckFailure("ssh_password_prompt_has_echo_enabled")
        secret = self.password if password is None else password
        # Extend detection before typing even an intentionally wrong password.
        process.guard = SecretGuard((*process.guard.values, secret))
        process.send(secret + b"\n")

    def login(self, **kwargs):
        process = self.process(**kwargs)
        try:
            self.password_prompt(process)
            process.expect(PROMPT, 20)
            return process
        except BaseException:
            process.close()
            raise


def marker(name, token):
    return f"NXRS_SSH_{name} token={token}".encode("ascii")


def require_stdio(output, token):
    for name in ("THREAD", "STDOUT", "STDERR", "SUCCESS"):
        if marker(name, token) not in output:
            raise CheckFailure("missing_rust_stdio_or_thread_marker")


def command(process, text):
    process.send(text.encode("ascii") + b"\n")


def result_code(process, expected):
    command(process, "echo $?")
    output, _ = process.expect(PROMPT)
    values = re.findall(rb"(?:^|[\r\n])(-?[0-9]+)[\r\n]", output)
    if values != [str(expected).encode("ascii")]:
        raise CheckFailure("nsh_command_result_mismatch")


def logout(process):
    command(process, "exit")
    if process.wait() != 0:
        raise CheckFailure("shell_exit_was_not_successful")


def assert_resource_stability(before, after, task_budget=0, fd_budget=0):
    for name, budget in (("tasks", task_budget), ("fd_rows", fd_budget)):
        if name not in before or name not in after:
            raise CheckFailure("resource_measurement_missing")
        if after[name] > before[name] + budget:
            raise CheckFailure(f"resource_growth_{name}")


class Suite:
    def __init__(self, client, out, console=None, evidence_class="device", selected_case=None):
        self.client, self.out, self.console = client, out, console
        self.results = []
        self.evidence_class = evidence_class
        self.selected_case = selected_case

    def case(self, name, operation):
        started = time.monotonic()
        try:
            details = operation() or {}
            result = {"name": name, "status": "passed", "details": details}
        except Exception as error:
            # Exceptions are classified without including peer output or secrets.
            code = str(error) if isinstance(error, CheckFailure) else type(error).__name__
            result = {"name": name, "status": "failed", "reason": code}
        result["seconds"] = round(time.monotonic() - started, 3)
        self.results.append(result)
        print(f"{name}: {result['status']}", flush=True)
        self.save()
        return result["status"] == "passed"

    def save(self):
        evidence = {
            "schema": 1, "evidence_class": self.evidence_class,
            "transport": "real_openssh_controlling_pty" if self.evidence_class == "device" else "host_control",
            "target": {"host": self.client.host, "port": self.client.port},
            "selected_case": self.selected_case,
            "overall": "failed" if any(case["status"] == "failed" for case in self.results)
                       else "selected_case_passed_full_suite_pending" if self.results and self.selected_case
                       else "checks_passed_full_qualification_pending" if self.results else "not_run",
            "cases": self.results,
            "pending": [
                "persistent_identity_across_reboot_and_readonly_storage",
                "empty_corrupt_unreadable_password_databases",
                "entropy_and_crypto_fault_injection_on_device",
                "measured_heap_stack_tcp_pty_budgets_and_peak_handshake_cost",
                "unavailable_bind_address_address_in_use_and_network_loss_recovery",
                "explicit_maximum_authentication_attempt_limit",
                "idle_timeout_on_device",
                "confirmed_output_backpressure_and_product_workload_latency",
                "device_fault_injection_for_missing_output_resource_leaks_and_incomplete_shutdown",
            ] + ([] if self.console else ["independent_lifecycle_progress_and_resource_observations"]),
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        (self.out / "summary.json").write_text(json.dumps(evidence, indent=2) + "\n")

    def wait_listening(self):
        if self.console:
            self.console.wait_listening()
        else:
            # The server rebinds after full cleanup. Retry only this bounded
            # readiness probe; a successful TCP open is not a successful login.
            time.sleep(0.3)

    def shell(self):
        self.wait_listening()
        with tempfile.TemporaryDirectory(prefix="nxrs-ssh-") as directory:
            control = Path(directory) / "master"
            process = self.client.login(control=control, master=True)
            try:
                token = secrets.token_hex(12)
                # Deliberately partial NSH command input.
                text = f"ssh_probe stdio {token}".encode("ascii")
                process.send(text[:8])
                time.sleep(0.1)
                process.send(text[8:] + b"\n")
                process.expect(re.escape(marker("READY", token)))
                process.send(f"input:{token}\n".encode("ascii"))
                output, _ = process.expect(PROMPT)
                require_stdio(output, token)
                result_code(process, 0)
                command(process, f"ssh_probe fail {token}")
                process.expect(re.escape(marker("EXPECTED_ERROR", token)))
                process.expect(PROMPT)
                # Pinned NSH reports builtin success/failure as 0/1; the native
                # task exits 7 but interactive "$?" does not preserve that code.
                result_code(process, 1)
                process.resize(37, 113)
                time.sleep(0.2)
                command(process, f"ssh_probe tty {token}")
                _, observed = process.expect(
                    re.escape(marker("TTY", token)) + rb" rows=([0-9]+) columns=([0-9]+)\b")
                if observed != (b"37", b"113"):
                    raise CheckFailure("remote_terminal_size_mismatch")
                process.expect(PROMPT)
                command(process, f"ssh_probe wait {token}")
                process.expect(re.escape(marker("WAIT", token)))
                process.send(b"\x03")
                process.expect(re.escape(marker("INTERRUPTED", token)), 5)
                process.expect(PROMPT)
                self.second_transport(process)
                self.second_channel(process, control)
                baseline_keys = process.newkeys
                before_ticks = self.console.progress() if self.console else None
                count = 8192
                command(process, f"ssh_probe bulk {token} {count}")
                time.sleep(1) # Deliberately stop consuming the host terminal.
                if self.console and self.console.progress() <= before_ticks:
                    raise CheckFailure("independent_progress_stalled_during_output")
                for sequence in range(count):
                    _, groups = process.expect(re.escape(marker("DATA", token)) + rb" sequence=(\d{5})\b", 30)
                    if int(groups[0]) != sequence:
                        raise CheckFailure("bulk_output_missing_duplicated_or_reordered")
                tail, _ = process.expect(re.escape(marker("BULK_DONE", token)) + f" lines={count}".encode())
                if marker("DATA", token) in tail:
                    raise CheckFailure("bulk_output_has_extra_rows")
                process.expect(PROMPT, 30)
                if process.newkeys <= baseline_keys:
                    raise CheckFailure("no_observed_rekey_after_bulk_output")
                logout(process)
                return {"bulk_lines": count, "newkeys_received": process.newkeys,
                        "negotiated_algorithms": process.algorithms,
                        "stderr_scope": "separate marker on the merged PTY", "ctrl_c": "explicit_fixture_SIGINT_handler"}
            finally:
                process.close()

    def second_transport(self, current):
        if self.console:
            self.console.require_state("session")
        try:
            connection = socket.create_connection((self.client.host, self.client.port), 2)
        except (ConnectionRefusedError, ConnectionResetError, TimeoutError):
            pass
        else:
            connection.close()
            raise CheckFailure("second_transport_admitted")
        command(current, "echo NXRS_SSH_PRIMARY_ALIVE")
        current.expect(rb"[\r\n]NXRS_SSH_PRIMARY_ALIVE[\r\n]")
        current.expect(PROMPT)

    def second_channel(self, current, control):
        process = self.client.process(control=control)
        try:
            process.wait(10)
            if process.returncode == 0 or not re.search(rb"mux_client_request_session.*(?:failed|refused)", process.pending, re.I):
                raise CheckFailure("second_channel_not_explicitly_refused_over_master")
        finally:
            process.close()
        command(current, "echo NXRS_SSH_PRIMARY_ALIVE")
        current.expect(rb"[\r\n]NXRS_SSH_PRIMARY_ALIVE[\r\n]")
        current.expect(PROMPT)

    def rejected_login(self, unknown=False):
        self.wait_listening()
        process = self.client.process(user=f"nxrs_missing_{secrets.token_hex(4)}" if unknown else None)
        try:
            wrong = secrets.token_urlsafe(24).encode("ascii") if not unknown else self.client.password
            self.client.password_prompt(process, wrong)
            process.wait(20)
            if process.returncode == 0 or b"Permission denied" not in process.pending:
                raise CheckFailure("login_was_not_explicitly_rejected")
        finally:
            process.close()

    def denied_request(self, kind):
        self.wait_listening()
        options = {
            "exec": {"command": ("echo", "NXRS_EXEC_MUST_NOT_RUN")},
            "scp": {"command": ("scp", "-t", "/tmp/nxrs-must-not-exist")},
            "sftp": {"extra": ("-s",), "command": ("sftp",)},
            "remote_forward": {"extra": ("-N", "-o", "ExitOnForwardFailure=yes", "-R", "0:127.0.0.1:9")},
            "direct_tcp": {"extra": ("-W", "127.0.0.1:9")},
        }
        process = self.client.process(tty=False, **options[kind])
        try:
            self.client.password_prompt(process)
            process.wait(15)
            refused = rb"(?:exec|subsystem) request failed|remote port forwarding failed|stdio forwarding failed|administratively prohibited"
            if process.returncode == 0 or not re.search(refused, process.pending, re.I):
                raise CheckFailure("request_was_not_explicitly_refused")
            if b"\nNXRS_EXEC_MUST_NOT_RUN\r" in process.pending:
                raise CheckFailure("forbidden_exec_command_ran")
        finally:
            process.close()

    def changed_key(self):
        self.wait_listening()
        with tempfile.TemporaryDirectory(prefix="nxrs-wrong-host-") as directory:
            key = Path(directory) / "wrong"
            subprocess.run(["ssh-keygen", "-q", "-t", "ecdsa", "-b", "256", "-N", "", "-f", str(key)],
                           check=True, capture_output=True)
            public = key.with_suffix(".pub").read_text().split()
            trusted = Path(directory) / "known_hosts"
            name = f"[{self.client.host}]:{self.client.port}" if self.client.port != 22 else self.client.host
            trusted.write_text(f"{name} {public[0]} {public[1]}\n")
            process = self.client.process(known_hosts=trusted)
            try:
                process.wait(15)
                if process.returncode == 0 or b"Host key verification failed" not in process.pending:
                    raise CheckFailure("changed_host_key_was_not_rejected")
                if re.search(rb"(?i)password:", process.pending):
                    raise CheckFailure("password_requested_after_untrusted_host_key")
            finally:
                process.close()

    def authentication_deadline(self, timeout):
        self.wait_listening()
        if not self.console:
            raise CheckFailure("auth_deadline_requires_independent_control")
        connected = time.monotonic()
        process = self.client.process()
        try:
            process.expect(rb"(?i)password: ?$", 20)
            self.console.require_state("session")
            started = time.monotonic()
            # OpenSSH may block reading /dev/tty even after the peer disconnects.
            # Keep the terminal quiet and observe reclamation independently.
            while time.monotonic() - started < timeout + 5:
                if self.console.state() == "listening":
                    reclaimed = time.monotonic()
                    if reclaimed - connected < timeout - 2:
                        raise CheckFailure("unauthenticated_session_ended_before_deadline")
                    break
                time.sleep(0.2)
            else:
                raise CheckFailure("authentication_deadline_not_observed")
        finally:
            process.close(abrupt=True)
        process = self.client.login()
        try:
            logout(process)
        finally:
            process.close()
        self.wait_listening()
        return {"observed_reclamation_seconds": round(reclaimed - started, 3), "fresh_login": True}

    def reconnect(self, count):
        before = self.console.snapshot("before-reconnect") if self.console else None
        for index in range(count):
            self.wait_listening()
            process = self.client.login()
            try:
                token = secrets.token_hex(12)
                command(process, f"echo NXRS_SSH_RECONNECT_{token}")
                process.expect(f"[\r\n]NXRS_SSH_RECONNECT_{token}[\r\n]".encode())
                process.expect(PROMPT)
                logout(process)
            finally:
                process.close()
            if index % 10 == 9:
                self.rejected_login()
        self.wait_listening()
        if self.console:
            after = self.console.snapshot("after-reconnect")
            assert_resource_stability(before, after)
            if after["ticks"] <= before["ticks"]:
                raise CheckFailure("independent_firmware_progress_stalled")
        return {"successful_sessions": count, "interleaved_failed_logins": count // 10,
                "same_boot": self.console is not None}

    def abrupt_disconnect(self):
        self.wait_listening()
        process = self.client.login()
        process.close(abrupt=True)
        self.wait_listening()
        process = self.client.login()
        try:
            logout(process)
        finally:
            process.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=2222)
    parser.add_argument("--user", required=True)
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--expected-fingerprint", required=True, help="SHA256 fingerprint verified through the trusted provisioning path")
    parser.add_argument("--password-file", type=Path, help="owned mode-0600 file outside this repository; otherwise prompt")
    parser.add_argument("--out", type=Path, default=ROOT / "target/ssh-qualification")
    parser.add_argument("--build-evidence", type=Path, help="ssh-build-evidence.json for the image the operator flashed")
    parser.add_argument("--reconnects", type=int, default=100)
    parser.add_argument("--auth-timeout", type=int, default=30)
    parser.add_argument("--case", choices=("pty", "auth-deadline", "active-stop"),
                        help="run one focused device case with normal startup and shutdown checks")
    parser.add_argument("--serial-port", help="independent local maintenance console; requires pyserial")
    parser.add_argument("--ssid")
    parser.add_argument("--wifi-password-file", type=Path)
    parser.add_argument("--provision-key", type=Path, help="new native Dropbear private key; provision only on an empty target")
    parser.add_argument("--tmpfs", action="store_true", help="explicitly mount disposable /mnt/ssh TMPFS; never persistence evidence")
    args = parser.parse_args()
    ipaddress.IPv4Address(args.host)
    if not 1 <= args.port <= 65535 or not 1 <= args.reconnects <= 10000:
        parser.error("invalid port or reconnect count")
    if args.auth_timeout <= 0:
        parser.error("authentication timeout must be positive")
    if args.case and not args.serial_port:
        parser.error("focused device cases require an independent serial console")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", args.user):
        parser.error("invalid username")
    trusted = verify_known_hosts(args.known_hosts, args.host, args.port, args.expected_fingerprint)
    if not args.password_file and not sys.stdin.isatty():
        parser.error("a terminal prompt or a private --password-file is required")
    password = password_file(args.password_file) if args.password_file else getpass.getpass("Disposable SSH password: ").encode("ascii")
    if not 8 <= len(password) <= 256 or not all(32 <= value <= 126 for value in password):
        parser.error("password must be 8..256 printable ASCII bytes")
    out = args.out / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(4))
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    lookup = f"[{args.host}]:{args.port}" if args.port != 22 else args.host
    records = subprocess.check_output(["ssh-keygen", "-F", lookup, "-f", str(trusted)])
    isolated = out / "known_hosts"
    isolated.write_bytes(b"\n".join(line for line in records.splitlines() if line and not line.startswith(b"#")) + b"\n")
    isolated.chmod(0o600)
    trusted = verify_known_hosts(isolated, args.host, args.port, args.expected_fingerprint)
    if args.build_evidence:
        build = json.loads(args.build_evidence.read_text())
        if build.get("evidence_class") != "crossbuild" or not build.get("ssh_enabled"):
            parser.error("--build-evidence must describe an SSH-enabled cross-build")
        (out / "ssh-build-evidence.json").write_text(json.dumps(build, indent=2) + "\n")
    console = None
    client = OpenSsh(args.host, args.port, args.user, trusted, password)
    suite = Suite(client, out, selected_case=args.case)
    try:
        if args.serial_port:
            from console import SerialLab
            console = SerialLab(args, password, out, sys.modules[__name__])
            suite.console = console
            if not suite.case("local_network_and_native_start", console.start):
                return 1
        elif args.ssid or args.wifi_password_file or args.provision_key or args.tmpfs:
            parser.error("network provisioning options require --serial-port")
        (out / "host-tools.json").write_text(json.dumps({
            "openssh": subprocess.run(["ssh", "-V"], capture_output=True, text=True).stderr.strip(),
            "expected_host_fingerprint": args.expected_fingerprint,
            "reconnects_requested": args.reconnects,
            "credential_capture": "none",
        }, indent=2) + "\n")
        focused = {
            "pty": ("pty_rust_stdio_threads_resize_interrupt_rekey_channel_limits", suite.shell),
            "auth-deadline": ("authentication_deadline", lambda: suite.authentication_deadline(args.auth_timeout)),
            "active-stop": ("active_stop_and_restart", lambda: console.active_stop(client)),
        }
        if args.case:
            suite.case(*focused[args.case])
        else:
            # Failure controls exercise the real peer and host-key verifier.
            suite.case("changed_host_key_rejected", suite.changed_key)
            suite.case("wrong_password_rejected", suite.rejected_login)
            suite.case("unknown_user_rejected", lambda: suite.rejected_login(True))
            suite.case(*focused["pty"])
            for kind in ("exec", "scp", "sftp", "remote_forward", "direct_tcp"):
                suite.case(kind + "_refused", lambda kind=kind: suite.denied_request(kind))
            suite.case("abrupt_client_death_and_reconnect", suite.abrupt_disconnect)
            if console:
                suite.case(*focused["auth-deadline"])
            suite.case("same_boot_reconnect_campaign", lambda: suite.reconnect(args.reconnects))
            if console:
                suite.case(*focused["active-stop"])
    finally:
        if console:
            suite.case("complete_local_shutdown", console.finish)
            console.close()
        suite.save()
        print(f"Evidence: {out / 'summary.json'}", flush=True)
    return int(any(result["status"] == "failed" for result in suite.results))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CheckFailure as error:
        raise SystemExit(str(error))
