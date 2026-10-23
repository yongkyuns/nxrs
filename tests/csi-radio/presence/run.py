#!/usr/bin/env python3
"""Full reference serial protocol. No discovery, flashing or reset operations."""
import importlib.util
import math
from pathlib import Path
import re
import time

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "idf_protocol", HERE.parent / "idf60/run.py"
)
IDF = importlib.util.module_from_spec(spec)
spec.loader.exec_module(IDF)
FIELDS = (
    "ms state motion init training ready calibrated wander jitter average threshold "
    "templates background advisory rf_freshness"
).split()
PROBE_FIELDS = (
    "windows bad_input bad_output kernel_errors uninspected wander_calls "
    "bad_template bad_current constant_template constant_current last_error error_cols error_rows0 error_rows1"
).split()


def probe(line):
    parts = line.decode("ascii").split()
    if not parts or parts[0] != "PRESENCE_PROBE":
        raise ValueError("not a numerical probe")
    pairs = [part.split("=", 1) for part in parts[1:]]
    if len(pairs) != len(PROBE_FIELDS) or any(len(pair) != 2 for pair in pairs):
        raise ValueError("numerical probe schema mismatch")
    result = dict(pairs)
    if set(result) != set(PROBE_FIELDS) or any(
        not re.fullmatch(
            r"-?[0-9]{1,10}" if key == "last_error" else r"[0-9]{1,10}", value
        )
        for key, value in result.items()
    ):
        raise ValueError("invalid numerical probe")
    return {
        "kind": "numerical_probe",
        **{key: int(value) for key, value in result.items()},
    }


def sample(line):
    if not line.startswith(b"PRESENCE_SAMPLE "):
        raise ValueError("not a presence sample")
    parts = line.decode("ascii").split()[1:]
    pairs = [part.split("=", 1) for part in parts]
    if len(pairs) != len(FIELDS) or any(len(pair) != 2 for pair in pairs):
        raise ValueError("presence sample schema mismatch")
    result = dict(pairs)
    if set(result) != set(FIELDS):
        raise ValueError("missing, duplicate or unexpected presence field")
    for key in (
        "ms",
        "motion",
        "init",
        "training",
        "ready",
        "calibrated",
        "templates",
        "background",
        "advisory",
    ):
        if not re.fullmatch(r"[0-9]{1,10}", result[key]):
            raise ValueError("invalid presence counter")
        result[key] = int(result[key])
    for key in ("wander", "jitter", "average", "threshold"):
        result[key] = float(result[key])
        if not math.isfinite(result[key]) or result[key] < 0:
            raise ValueError("invalid presence numerical result")
    if (
        any(result[key] not in (0, 1) for key in ("training", "ready", "calibrated"))
        or result["motion"] not in range(4)
        or result["init"] not in range(3)
        or result["advisory"] != 1
        or result["rf_freshness"] != "not_exported"
    ):
        raise ValueError("invalid presence status")
    available = not result["training"] and result["ready"] and result["calibrated"]
    if result["state"] not in ("unknown", "presence_hint", "not_detected"):
        raise ValueError("invalid presence state")
    if (result["state"] != "unknown") != bool(available):
        raise ValueError("uncalibrated or warming state treated as detection")
    return result


class Client:
    def __init__(self, console):
        self.console = console
        self.protocol = IDF.Client(console)
        self.secrets = ()
        self.last_ms = None

    def line(self, deadline):
        line = self.protocol.line(deadline, self.secrets)
        if line.startswith(b"PRESENCE_FAIL"):
            raise RuntimeError("presence reference failed; diagnostic withheld")
        return line

    def start(self, ssid, password, seconds, amplitude="log"):
        if not 30 <= seconds <= 600:
            raise ValueError("presence duration outside bound")
        if amplitude not in ("log", "linear"):
            raise ValueError("unsupported amplitude case")
        frame, self.secrets = IDF.credential_frame(ssid, password)
        deadline = time.monotonic() + 45
        while self.line(deadline) != b"PRESENCE_COMMAND_READY force=0":
            pass
        IDF.RADIO.write_console(
            self.console, f"PRESENCE {seconds} {amplitude}\n".encode()
        )
        expected = f"PRESENCE_OPTIONS seconds={seconds} force=0 engine=espressif_full amplitude={amplitude}".encode()
        matched = False
        while True:
            line = self.line(deadline)
            if line.startswith(b"PRESENCE_OPTIONS "):
                if matched or line != expected:
                    raise RuntimeError(
                        "presence options mismatch; credentials withheld"
                    )
                matched = True
            if line == b"PRESENCE_CREDENTIALS_READY":
                if not matched:
                    raise RuntimeError("presence options missing; credentials withheld")
                break
        IDF.RADIO.write_console(self.console, frame)
        deadline = time.monotonic() + 35
        expected_begin = re.compile(
            rb"PRESENCE_BEGIN seconds="
            + str(seconds).encode()
            + rb" channel=(?:[1-9]|1[0-4]) calibration=required rf_freshness=not_exported"
        )
        while True:
            line = self.line(deadline)
            if line.startswith(b"PRESENCE_BEGIN "):
                if not expected_begin.fullmatch(line):
                    raise RuntimeError("presence begin mismatch")
                return

    def command(self, value):
        if value not in ("TRAIN_START", "TRAIN_STOP", "END"):
            raise ValueError("unsupported reference control")
        IDF.RADIO.write_console(self.console, (value + "\n").encode())

    def read(self, deadline):
        line = self.line(deadline)
        if line.startswith(b"PRESENCE_PROBE "):
            return probe(line)
        if line.startswith(b"PRESENCE_SAMPLE "):
            value = sample(line)
            if self.last_ms is not None and value["ms"] <= self.last_ms:
                raise ValueError("duplicate or reversed report clock")
            self.last_ms = value["ms"]
            return value
        if line.startswith(
            (b"PRESENCE_TRAIN_START ", b"PRESENCE_TRAIN_STOP ", b"PRESENCE_END ")
        ):
            return line.decode("ascii")
        return None
