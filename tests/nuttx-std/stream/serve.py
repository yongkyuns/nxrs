#!/usr/bin/env python3
"""Host-assisted continuous inspection; not a replacement for firmware qualification.

Keep one console open and recover USB disconnects without flashing or resetting
hardware. Requires continuous-mode firmware. Real firmware failures stop supervision.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

SPEC = importlib.util.spec_from_file_location("stream_qualification", Path(__file__).with_name("run.py"))
RUN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUN)


class Session:
    def __init__(self, console, credentials, seconds, emit, stop=None, clock=time.monotonic, tls=False):
        if seconds is not None and not 10 <= seconds <= 3600:
            raise ValueError("cycle seconds must be 10–3600")
        self.console, self.credentials, self.seconds = console, credentials, seconds
        self.emit, self.clock = emit, clock
        self.tls = tls
        self.stop = stop or threading.Event()
        self.pending = bytearray()
        self.completed = 0

    def read(self):
        self.pending.extend(self.console.read(1024))
        password = self.credentials[1]
        if password and password in self.pending:
            self.emit("credential_echo_blocked")
            raise RuntimeError("credential echo detected; output withheld")
        if len(self.pending) > RUN.SERIAL_LIMIT:
            raise RuntimeError("console output exceeded bound")

    def start(self):
        ssid, password = self.credentials
        duration = "continuous" if self.seconds is None else str(self.seconds)
        mode = "tls " if self.tls else ""
        self.console.write(f"stream_test {mode}{duration} 0 320 240 81\n".encode("ascii"))
        RUN.wait_stage(self.console, self.pending, b"NXRS_WIFI_CREDENTIALS_READY", password, 15, "credential reader")
        payload = bytes((len(ssid), len(password))) + ssid + password
        if self.console.write(payload) != len(payload):
            raise RuntimeError("incomplete credential write; output withheld")
        self.emit("starting")

    def run(self, cycles=0):
        # Sending a blank line is safe both at NSH and while stream_test owns stdin.
        # On reconnect, adopt a still-running command rather than starting a second.
        self.console.write(b"\n")
        deadline = self.clock() + 60
        active, passed, completed, stop_sent = False, False, 0, False
        while True:
            if self.stop.is_set() and active and self.seconds is None and not stop_sent:
                self.console.write(b"stop\n")
                stop_sent = True
                self.emit("stopping")
                deadline = self.clock() + 45
            self.read()
            if any(marker in self.pending for marker in RUN.FAIL_MARKERS):
                # Never continue past a panic or conceal it as normal renewal.
                failure = next((line.strip(b"\r") for line in self.pending.split(b"\n")
                                if any(marker in line for marker in RUN.FAIL_MARKERS)), b"")
                self.emit("firmware_failure", reason=RUN.device_failure(failure))
                raise RuntimeError("firmware failure; raw console output withheld")
            prompt = b"nsh> " in self.pending
            while b"\n" in self.pending:
                line, _, rest = self.pending.partition(b"\n")
                self.pending[:] = rest
                line = line.strip(b"\r\t ")
                report = RUN.REPORT_RE.fullmatch(line)
                online = RUN.ONLINE_RE.fullmatch(line)
                if report:
                    active = True
                    if not stop_sent:
                        deadline = self.clock() + 30
                    self.emit("telemetry", values=[int(x) if x is not None else None for x in report.groups()])
                elif online:
                    active = True
                    if not stop_sent:
                        deadline = self.clock() + 30
                    self.emit("online", ip=online[1].decode("ascii"), port=int(online[2]))
                elif line.startswith(b"NXRS_DETECTION_REPORT"):
                    # Preserve only the bounded numeric/enum schema, never raw
                    # console content. Detection does not renew camera health.
                    self.emit("detection", detection=RUN.detection_report(line))
                elif line.startswith(b"NXRS_STREAM_STACK"):
                    self.emit("owner_stack", stack=RUN.owner_stack(line))
                elif RUN.PASS_RE.fullmatch(line):
                    active, passed = False, True
                    completed += 1
                    self.completed = completed
                    self.emit("clean_shutdown", completed_cycles=completed)
                    deadline = self.clock() + 15
                elif b"*** Booting NuttX ***" in line:
                    self.emit("boot_observed")
            if prompt:
                # A prompt is required before credentials or another command.
                self.pending.clear()
                if active:
                    self.emit("unexpected_shell", stream_was_active=True)
                    if stop_sent:
                        raise RuntimeError("device exited without confirming clean shutdown")
                if self.stop.is_set() or (cycles and completed >= cycles):
                    return completed
                if passed:
                    self.emit("renewing")
                self.start()
                active, passed = True, False
                deadline = self.clock() + 60
            if self.clock() >= deadline:
                self.emit("console_stalled")
                raise RuntimeError("camera console stalled; no automatic hardware reset attempted")


class State:
    def __init__(self, path):
        self.path = RUN._outside_repo(path)
        # Reserve a new private output, never overwrite an unrelated file.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.data = {"pid": os.getpid(), "boot_observations": 0, "usb_reconnections": 0,
                     "renewals": 0, "source": "physical", "status": "starting"}

    def emit(self, event, **fields):
        if event == "tls":
            self.data.update(tls=fields['tls'], tls_received_unix=time.time())
        elif event == "detection":
            self.data.update(detection=fields['detection'], detection_received_unix=time.time())
        elif event == "owner_stack":
            self.data.update(owner_stack=fields['stack'], owner_stack_received_unix=time.time())
        else:
            self.data.update(status=event, updated_unix=time.time(), **fields)
        if event in ("starting", "renewing", "boot_observed", "usb_disconnected", "unexpected_shell",
                     "stopping", "stopped", "clean_shutdown", "failed", "firmware_failure",
                     "console_stalled", "credential_echo_blocked") and 'detection' in self.data:
            self.data['detection'] = dict(self.data['detection'], state='Unavailable',
                                          sequence=0, age_ms=0, score=0)
        if event in ("firmware_failure", "console_stalled", "credential_echo_blocked"):
            self.data["last_fault"] = {"event": event, **fields}
        counter = {"boot_observed": "boot_observations", "usb_reconnected": "usb_reconnections",
                   "renewing": "renewals"}.get(event)
        if counter:
            self.data[counter] += 1
        with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as f:
            try:
                json.dump(self.data, f, indent=2)
                f.close()
                os.replace(f.name, self.path)
            finally:
                Path(f.name).unlink(missing_ok=True)
        if event not in ("telemetry", "detection", "owner_stack"):
            print(json.dumps({"event": event, **fields}), flush=True)


def identity(port):
    from serial.tools import list_ports
    for item in list_ports.comports():
        if item.device == port:
            if not item.serial_number or item.vid is None or item.pid is None:
                raise ValueError("device needs a stable USB serial identity")
            return item.vid, item.pid, item.serial_number
    return None


def supervise(port, credentials, seconds, cycles, emit, stop):
    import serial
    expected = identity(port)
    if expected is None:
        raise ValueError("selected USB device is absent")
    remaining = cycles
    reconnecting = False
    disconnects = 0
    while not stop.is_set():
        console = serial.Serial(port=None, baudrate=115200, timeout=.1, write_timeout=5)
        console.dtr = console.rts = False
        session = None
        try:
            if identity(port) != expected:
                raise RuntimeError("USB device identity changed; refusing to send credentials")
            console.port = port
            console.open()
            if reconnecting:
                emit("usb_reconnected")
            session = Session(console, credentials, seconds, emit, stop)
            completed = session.run(remaining)
            if remaining:
                remaining -= completed
                if remaining <= 0:
                    return
        except serial.SerialException:
            disconnects += 1
            emit("usb_disconnected")
            console.close()
            if remaining and session:
                remaining -= session.completed
                if remaining <= 0:
                    return
            if disconnects >= 10:
                raise RuntimeError("repeated USB faults; session stopped for diagnosis")
            # Preserve the fault and wait; do not reset the board or send credentials
            # to a replacement device. One minute bounds each disconnect attempt.
            deadline = time.monotonic() + 60
            stop.wait(1)
            while not stop.is_set():
                found = identity(port)
                if found is not None:
                    if found != expected:
                        raise RuntimeError("USB device identity changed")
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("camera USB did not return within 60 seconds")
                stop.wait(1)
            reconnecting = True
        finally:
            console.close()


def supervise_tls(port, credentials, configuration, emit, stop, *, renew_after=45 * 60):
    """Supervise the USB development enrollment and the actual Rust ingest.

    Renew through ordinary joined camera sessions before the one-hour CRL
    expires. The phone enrollment, media server and control gateway are separate.
    USB loss or a real firmware/ingest fault stops supervision, without a reset.
    """
    import ipaddress
    import re
    import serial
    if not 10 <= renew_after <= 45 * 60:
        raise ValueError("TLS renewal must precede the one-hour CRL deadline")
    spec = importlib.util.spec_from_file_location("tls_provision", Path(__file__).with_name("tls_provision.py"))
    tls = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tls)
    path = RUN._outside_repo(configuration)
    metadata = path.stat()
    if not path.is_file() or metadata.st_mode & 0o077 or metadata.st_size > 16384:
        raise ValueError("TLS configuration must be a bounded private file")
    config = json.loads(path.read_text())
    template = config['ingest']
    address, port_number = template['camera'].rsplit(':', 1)
    if not ipaddress.IPv4Address(address).is_private or port_number != '8443' or template['allow_insecure_camera_http']:
        raise ValueError("TLS ingest requires a private camera and port 8443")
    enrollment = template['camera_tls']
    root = RUN._outside_repo(config['enrollment_directory'])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o077:
        raise ValueError("TLS enrollment directory must be private")
    program = Path(config['ingest_program'])
    if not program.is_absolute() or not program.is_file():
        raise ValueError("Rust ingest requires an absolute executable path")
    expected = identity(port)
    if expected is None:
        raise ValueError("selected USB device is absent")
    console = serial.Serial(port=None, baudrate=115200, timeout=.05, write_timeout=5)
    console.dtr = console.rts = False
    console.port, console.exclusive = port, True
    # Keep the original USB connection through renewals; closing it can reset
    # this board before the next host operation sees its NSH prompt.
    with console:
        while not stop.is_set():
            if identity(port) != expected:
                raise RuntimeError("USB device identity changed")
            with tempfile.TemporaryDirectory(prefix='enrollment-', dir=root) as directory:
                session_root = Path(directory)
                supervisor, child, log = None, None, None
                passed = False
                try:
                    console.write(b'\n')
                    RUN.wait_stage(console, bytearray(), b'nsh> ', credentials[1], 30, 'NSH')
                    Session(console, credentials, None, emit, stop, tls=True).start()
                    def observed(line):
                        raw = line.encode('ascii')
                        report = RUN.REPORT_RE.fullmatch(raw)
                        online = RUN.ONLINE_RE.fullmatch(raw)
                        if report:
                            emit('telemetry', values=[int(v) if v is not None else None for v in report.groups()])
                        elif online:
                            emit('online', ip=online[1].decode('ascii'), port=int(online[2]))
                        elif raw.startswith(b'NXRS_DETECTION_REPORT'):
                            emit('detection', detection=RUN.detection_report(raw))
                        elif raw.startswith(b'NXRS_STREAM_STACK'):
                            emit('owner_stack', stack=RUN.owner_stack(raw))
                        elif line.startswith('NXRS_TLS_REPORT'):
                            fields = re.findall(r'(\w+)=(\d+)', line)
                            emit('tls', tls={key: int(value) for key, value in fields})
                    supervisor = tls.Supervisor(console, session_root, address,
                        site=enrollment['site_id'], camera=enrollment['camera_id'],
                        gateway=enrollment['gateway_id'], observer=observed)
                    if not supervisor.ready.wait(tls.INIT_TIMEOUT) or supervisor.failure:
                        raise RuntimeError("TLS enrollment/startup failed")
                    rendered = dict(template, camera_tls=supervisor.credentials.config())
                    ingest_config = root/'ingest.json'
                    tls.private(ingest_config, json.dumps(rendered))
                    subprocess.run([str(program), str(ingest_config), '--require-camera-tls', '--check-config'],
                        check=True, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    log = (root/'ingest.log').open('ab')
                    os.chmod(log.name, 0o600)
                    child = subprocess.Popen([str(program), str(ingest_config), '--require-camera-tls'],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
                    emit('enrolled', ip=address, port=8443, ingest_pid=child.pid, ingest_config=str(ingest_config))
                    deadline = time.monotonic() + renew_after
                    while not stop.wait(.1) and time.monotonic() < deadline:
                        if supervisor.finished.is_set() or child.poll() is not None:
                            raise RuntimeError("TLS camera or Rust ingest stopped unexpectedly")
                    passed = True
                finally:
                    try:
                        if child and child.poll() is None:
                            child.terminate()
                            try:
                                child.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                child.kill()
                                child.wait(timeout=5)
                                raise RuntimeError("Rust ingest failed to join") from None
                    finally:
                        try:
                            if supervisor:
                                try:
                                    if supervisor.ready.is_set() and not supervisor.finished.is_set() and not supervisor.failure:
                                        supervisor.request_stop()
                                        # Admission requests time even when there is no viewer.
                                        try:
                                            with socket.create_connection((address, 8443), timeout=2):
                                                pass
                                        except OSError:
                                            pass
                                        if not supervisor.finished.wait(45):
                                            raise RuntimeError("TLS camera shutdown deadline")
                                    joined = any(line in supervisor.events for line in (
                                        'NXRS_STREAM_PASS stopped=1 joined=1',
                                        'NXRS_TLS_CAMERA_PASS stopped=1 joined=1'))
                                    if passed and (supervisor.failure or not joined):
                                        raise RuntimeError("TLS camera did not confirm joined shutdown")
                                finally:
                                    supervisor.stop()
                        finally:
                            if log:
                                log.close()
            if not stop.is_set():
                emit('renewing')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--wifi-file", required=True)
    parser.add_argument("--state-file", required=True, help="new private JSON file outside the checkout")
    parser.add_argument("--tls-config", help="private USB enrollment and Rust ingest configuration")
    args = parser.parse_args()
    credentials = RUN.read_wifi_file(args.wifi_file)
    state = State(args.state_file)
    stop = threading.Event()
    def stopping(*_):
        stop.set()
    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    awake = None
    try:
        if sys.platform == "darwin":
            awake = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        if args.tls_config:
            supervise_tls(args.port, credentials, args.tls_config, state.emit, stop)
        else:
            supervise(args.port, credentials, None, 0, state.emit, stop)
        state.emit("stopped")
    except Exception:
        # Deliberately do not interpolate SDK/serial exceptions or raw console data.
        state.emit("failed", reason="session fault; inspect sanitized events; credentials withheld")
        raise SystemExit(1) from None
    finally:
        if awake is not None:
            awake.terminate()
            awake.wait(timeout=5)


if __name__ == "__main__":
    main()
