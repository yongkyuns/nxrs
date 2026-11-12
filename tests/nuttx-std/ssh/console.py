"""Independent physical-console owner for the qualification-only Rust lab."""
import re
import secrets
import struct
import time


class SerialLab:
    def __init__(self, args, password, out, api):
        # Import only when hardware was explicitly requested.
        import serial
        self.args, self.password, self.out, self.api = args, password, out, api
        if not args.ssid or not args.wifi_password_file:
            raise api.CheckFailure("serial_lab_requires_ssid_and_wifi_password_file")
        self.ssid = args.ssid.encode("utf-8")
        self.wifi_password = api.secret_file(args.wifi_password_file, 63).removesuffix(b"\n")
        if not 1 <= len(self.ssid) <= 32 or not 8 <= len(self.wifi_password) <= 63:
            raise api.CheckFailure("invalid_wifi_credentials")
        if not all(32 <= value <= 126 for value in self.wifi_password):
            raise api.CheckFailure("invalid_wifi_password_encoding")
        self.key = api.secret_file(args.provision_key, 4096) if args.provision_key else b""
        if self.key and not 32 <= len(self.key) <= 4096:
            raise api.CheckFailure("invalid_native_host_key_length")
        self.guard = api.SecretGuard((password, self.wifi_password, self.key))
        self.pending = bytearray()
        self.event_line = bytearray()
        self.server_exits = 0
        self.starts = 0
        self.entered = False
        self.initial = None
        self.connection = serial.Serial()
        self.connection.port = args.serial_port
        self.connection.baudrate = 115200
        self.connection.timeout = 0.1
        self.connection.write_timeout = 5
        # Opening a USB UART must not reset the target mid-campaign.
        self.connection.dtr = False
        self.connection.rts = False
        self.connection.open()

    def send(self, data):
        if self.connection.write(data) != len(data):
            raise self.api.CheckFailure("serial_short_write")
        self.connection.flush()

    def send_secret_frame(self, data):
        # The ESP32-S3 USB serial receive ring is 64 bytes. Give the target
        # time to drain it between writes, including when it is servicing Wi-Fi.
        for offset in range(0, len(data), 24):
            if offset:
                time.sleep(0.03)
            self.send(data[offset:offset + 24])

    def read_until(self, pattern, timeout=15):
        regex = re.compile(pattern, re.M)
        deadline = time.monotonic() + timeout
        while True:
            match = regex.search(self.pending)
            if match:
                output, groups = bytes(self.pending[:match.end()]), match.groups()
                del self.pending[:match.end()]
                return output, groups
            if time.monotonic() >= deadline:
                raise self.api.CheckFailure("maintenance_console_marker_timeout")
            value = self.connection.read(4096)
            self.guard.feed(value)
            self.pending.extend(value)
            self.event_line.extend(value)
            while b"\n" in self.event_line:
                line, _, tail = self.event_line.partition(b"\n")
                self.event_line = bytearray(tail)
                if b"NXRS_SSH_LAB_SERVER_EXIT result=" in line:
                    self.server_exits += 1
            if len(self.event_line) > 16384 or len(self.pending) > 2 * 1024 * 1024:
                raise self.api.CheckFailure("bounded_maintenance_output_exceeded")

    def shell_command(self, value):
        command = value.encode("ascii")
        self.send(command + b"\n")
        # Discard any queued wake-up prompts before this command's echo.
        self.read_until(re.escape(command) + rb"\r?\n")
        return self.read_until(self.api.PROMPT)[0]

    def shell_status(self, value):
        self.shell_command(value)
        output = self.shell_command("echo $?")
        values = re.findall(rb"(?:^|[\r\n])(-?[0-9]+)[\r\n]", output)
        if len(values) != 1:
            raise self.api.CheckFailure("maintenance_command_status_missing")
        return int(values[0])

    def start(self):
        self.send(b"\n\n\n")
        self.read_until(self.api.PROMPT)
        if self.args.tmpfs:
            # Operator explicitly requested disposable storage. Mount errors
            # remain visible through provisioning; this is not a persistent FS.
            self.shell_command("mkdir /mnt")
            self.shell_command("mkdir /mnt/ssh")
            if self.shell_status("mount -t tmpfs /mnt/ssh") != 0:
                raise self.api.CheckFailure("explicit_disposable_tmpfs_mount_failed")
        if self.shell_status("ls /proc/meminfo") != 0:
            self.shell_command("mkdir /proc")
            if self.shell_status("mount -t procfs /proc") != 0:
                raise self.api.CheckFailure("procfs_mount_failed")
        if self.shell_status("ifup wlan0") != 0:
            raise self.api.CheckFailure("wifi_interface_up_failed")
        self.send(b"ssh_probe lab\n")
        self.read_until(rb"NXRS_WIFI_CREDENTIALS_READY\r?\n")
        self.send_secret_frame(bytes((len(self.ssid), len(self.wifi_password))) + self.ssid + self.wifi_password)
        self.read_until(rb"NXRS_WIFI_ASSOCIATED_DHCP\r?\n", 40)
        self.read_until(rb"NXRS_SSH_LAB_READY[^\r\n]*[\r\n]")
        self.entered = True
        if self.key:
            self.send(b"provision\n")
            self.read_until(rb"NXRS_SSH_PROVISION_READY\r?\n")
            user = self.args.user.encode("ascii")
            frame = struct.pack("!IBH", len(self.key), len(user), len(self.password)) + self.key + user + self.password
            self.send_secret_frame(frame)
            output, fields = self.read_until(rb"NXRS_SSH_PROVISION_(DONE|FAIL)[^\r\n]*[\r\n]", 40)
            if b"NXRS_SSH_PROVISION_CONSOLE_RESTORE_FAIL" in output:
                raise self.api.CheckFailure("provision_console_restore_failed")
            if fields[0] == b"FAIL":
                raise self.api.CheckFailure("native_provision_failed")
        self.initial = self.snapshot("before-start")
        self.start_server()
        return {"wifi": "existing_station_owner", "storage": "disposable_tmpfs" if self.args.tmpfs else "operator_mounted",
                "identity": "newly_provisioned" if self.key else "existing", "native_launch": "foreground_builtin_child"}

    def start_server(self):
        self.send(b"start\n")
        self.read_until(rb"NXRS_SSH_LAB_STARTED pid=[0-9]+\r?\n")
        self.starts += 1
        self.wait_listening()

    def state(self):
        self.send(b"status\n")
        output = self.control_result(b"status")
        states = re.findall(rb"dropbear: state=([a-z]+) pid=-?[0-9]+ result=(-?[0-9]+)", output)
        if len(states) != 1:
            raise self.api.CheckFailure("native_status_missing_or_ambiguous")
        state, result = states[0]
        if state == b"failed":
            raise self.api.CheckFailure(f"native_server_failed:{int(result)}")
        return state.decode("ascii")

    def control_result(self, action, timeout=15):
        output, fields = self.read_until(
            rb"NXRS_SSH_LAB_CONTROL action=" + action + rb" result=(-?[0-9]+)\r?\n", timeout)
        result = int(fields[0])
        if result != 0:
            raise self.api.CheckFailure(f"native_{action.decode('ascii')}_failed:{result}")
        return output

    def require_state(self, expected):
        if self.state() != expected:
            raise self.api.CheckFailure("native_state_mismatch")

    def wait_listening(self):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            state = self.state()
            if state == "listening":
                return
            if state == "stopped":
                raise self.api.CheckFailure("native_server_failed_before_listening")
            time.sleep(0.1)
        raise self.api.CheckFailure("native_listener_readiness_timeout")

    def stop(self):
        self.send(b"stop\n")
        self.control_result(b"stop", 20)
        self.require_state("stopped")
        if self.server_exits < self.starts:
            # Reaping belongs to the Rust owner, separate from the daemon's
            # stopped state. Require both observations before releasing Wi-Fi.
            self.read_until(rb"NXRS_SSH_LAB_SERVER_EXIT result=-?[0-9]+\r?\n", 5)
        if self.server_exits < self.starts:
            raise self.api.CheckFailure("native_child_was_not_reaped")

    def progress(self):
        self.send(b"progress\n")
        _, values = self.read_until(rb"NXRS_SSH_LAB_PROGRESS ticks=([0-9]+)\r?\n")
        return int(values[0])

    def snapshot(self, name):
        self.send(b"snapshot\n")
        output, _ = self.read_until(rb"NXRS_SSH_SNAPSHOT_END ticks=[0-9]+\r?\n", 20)
        start = output.find(b"NXRS_SSH_SNAPSHOT_BEGIN")
        if start < 0 or b"NXRS_SSH_SNAPSHOT_FAIL" in output:
            raise self.api.CheckFailure("incomplete_resource_snapshot")
        snapshot = output[start:]
        tasks = re.search(rb"NXRS_SSH_TASKS count=([0-9]+)", snapshot)
        ticks = re.search(rb"NXRS_SSH_SNAPSHOT_BEGIN ticks=([0-9]+)", snapshot)
        if not tasks or not ticks or b"/group/fd" not in snapshot:
            raise self.api.CheckFailure("missing_task_or_descriptor_evidence")
        # FD rows are counted per task, including shared groups. This is an
        # intentionally conservative same-state witness, not a unique-FD total.
        fd_rows = len(re.findall(rb"^\s*[0-9]+\s+[0-9]+\s+[0-9a-fA-F]+\s+-?[0-9]+\s+", snapshot, re.M))
        if fd_rows == 0 or b"NXRS_SSH_PROC_UNAVAILABLE" in snapshot:
            raise self.api.CheckFailure("incomplete_fd_or_stack_evidence")
        (self.out / f"{name}.txt").write_bytes(snapshot)
        return {"tasks": int(tasks[1]), "fd_rows": fd_rows, "ticks": int(ticks[1])}

    def active_stop(self, client):
        self.wait_listening()
        process = client.login()
        try:
            self.require_state("session")
            token = secrets.token_hex(12)
            self.api.command(process, f"ssh_probe wait {token}")
            process.expect(re.escape(self.api.marker("WAIT", token)))
            self.stop()
            process.wait(10)
        finally:
            process.close()
        self.start_server()
        process = client.login()
        try:
            self.api.logout(process)
        finally:
            process.close()
        self.wait_listening()
        return {"generation_restarted": True, "old_session_reclaimed": True,
                "foreground_builtin": "cooperative_Rust_SIGINT_waiter"}

    def finish(self):
        if not self.entered:
            raise self.api.CheckFailure("local_owner_did_not_reach_ready_cleanup_needs_operator")
        self.stop()
        final = self.snapshot("after-stop")
        if self.initial:
            self.api.assert_resource_stability(self.initial, final)
        self.send(b"quit\n")
        self.read_until(rb"NXRS_SSH_LAB_DONE native=joined wifi=disconnected_and_joined\r?\n", 40)
        self.read_until(self.api.PROMPT)
        self.entered = False
        return {"native": "stopped_and_reaped", "wifi": "disconnected_and_joined", "local_nsh": "returned"}

    def close(self):
        self.connection.close()
