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
import subprocess
import sys
import tempfile
import threading
import time

SPEC = importlib.util.spec_from_file_location("stream_qualification", Path(__file__).with_name("run.py"))
RUN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUN)


class Session:
    def __init__(self, console, credentials, seconds, emit, stop=None, clock=time.monotonic):
        if seconds is not None and not 10 <= seconds <= 3600:
            raise ValueError("cycle seconds must be 10–3600")
        self.console, self.credentials, self.seconds = console, credentials, seconds
        self.emit, self.clock = emit, clock
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
        self.console.write(f"stream_test {duration} 0 320 240 81\n".encode("ascii"))
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
        self.data.update(status=event, updated_unix=time.time(), **fields)
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
        if event != "telemetry":
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--wifi-file", required=True)
    parser.add_argument("--state-file", required=True, help="new private JSON file outside the checkout")
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
