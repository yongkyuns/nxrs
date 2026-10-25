#!/usr/bin/env python3
"""Record from an explicitly selected, already-flashed CSI console. Never flash,
reset, discover devices or take over an existing camera supervisor's port."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "presence_lab_console", HERE.parent / "csi-radio/presence/run.py"
)
PROTOCOL = importlib.util.module_from_spec(spec)
spec.loader.exec_module(PROTOCOL)
CONTROLS = {
    "TRAIN_START",
    "TRAIN_STOP",
    "CALIBRATE_BACKGROUND",
    "CALIBRATE_VALIDATE",
    "CALIBRATE_ACCEPT",
    "LABEL_EMPTY",
    "LABEL_WALKING",
    "LABEL_SEATED",
    "LABEL_UNKNOWN",
    "END",
}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", required=True)
    p.add_argument(
        "--credentials",
        type=Path,
        required=True,
        help="private JSON with ssid/password; never printed",
    )
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--csi-lab", type=Path, required=True, help="built host executable")
    p.add_argument("--seconds", type=int, default=600)
    a = p.parse_args()
    if not 30 <= a.seconds <= 600 or a.credentials.stat().st_mode & 0o077:
        p.error("duration must be 30..600; credentials must be owner-only")
    if a.output.exists():
        p.error("output already exists")
    credentials = json.loads(a.credentials.read_text())
    import serial

    # PySerial's exclusive open prevents two compliant console owners. Do not
    # assert DTR/RTS; automatic reset is explicitly outside this app's contract.
    owner = subprocess.run(
        ["lsof", "-t", a.port], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    if owner.returncode not in (0, 1) or owner.stdout.strip():
        raise RuntimeError("console already owned or ownership unverifiable")
    console = serial.Serial(port=None, baudrate=115200, timeout=0.05, exclusive=True)
    console.dtr = False
    console.rts = False
    console.port = a.port
    capture = subprocess.Popen(
        [str(a.csi_lab), "record", str(a.manifest), str(a.output)],
        stdin=subprocess.PIPE,
    )
    controls = selectors.DefaultSelector()
    controls.register(sys.stdin, selectors.EVENT_READ)
    try:
        console.open()
        client = PROTOCOL.Client(console)

        def sink(line):
            capture.stdin.write(line)
            capture.stdin.flush()

        client.start(
            credentials["ssid"],
            credentials["password"],
            a.seconds,
            "linear",
            snapshots=True,
            trace=True,
            calibrated=True,
            diagnostic_pairs=True,
            numeric_failures=True,
            record_sink=sink,
        )
        del credentials
        print(
            "CSI recording active. Send TRAIN_START/STOP, calibration controls, LABEL_EMPTY/WALKING/SEATED/UNKNOWN or END. No automatic labels or calibration.",
            file=sys.stderr,
        )
        deadline = time.monotonic() + a.seconds + 15
        while time.monotonic() < deadline:
            if capture.poll() not in (None, 0):
                raise RuntimeError("recorder process failed")
            if controls.select(0):
                command = sys.stdin.readline()
                if not command:
                    controls.unregister(sys.stdin)
                elif command.strip() in CONTROLS:
                    client.command(command.strip())
                else:
                    raise ValueError("unsupported control")
            try:
                result = client.read(min(deadline, time.monotonic() + 0.1))
            except TimeoutError:
                continue
            if isinstance(result, str) and result.startswith("PRESENCE_END "):
                break
        else:
            raise TimeoutError("recording did not finish")
        capture.stdin.close()
        if capture.wait(timeout=30) != 0:
            raise RuntimeError("capture invalid; retain partial file for diagnosis")
    finally:
        controls.close()
        console.close()
        if capture.stdin and not capture.stdin.closed:
            capture.stdin.close()
        if capture.poll() is None:
            capture.terminate()
            capture.wait(timeout=5)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Native exceptions may contain USB device paths, never credentials.
        # Do not echo arbitrary serial lines or credential-bearing exceptions.
        print(
            "CSI collection failed: "
            + type(error).__name__
            + "; capture is incomplete until validated",
            file=sys.stderr,
        )
        sys.exit(1)
