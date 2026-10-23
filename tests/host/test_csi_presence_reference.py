"""Host-only protocol and source-provenance gates; no RF accuracy claim."""

import importlib.util
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUN = load("presence_protocol", ROOT / "tests/csi-radio/presence/run.py")
BUILD = load("presence_builder", ROOT / "tests/csi-radio/presence/build.py")
SAMPLE = (
    b"PRESENCE_SAMPLE ms=1000 state=presence_hint motion=2 init=2 training=0 "
    b"ready=1 calibrated=1 wander=0.02000000 jitter=0.03000000 average=0.02000000 "
    b"threshold=0.00100000 templates=2 background=20 advisory=1 rf_freshness=not_exported"
)


class Console:
    def __init__(self, lines):
        self.data = bytearray(b"\n".join(lines) + b"\n")
        self.writes = []

    def read(self, size):
        value = bytes(self.data[:size])
        del self.data[:size]
        return value

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        pass


class PresenceReferenceTests(unittest.TestCase):
    def test_calibrated_hint_is_explicitly_advisory(self):
        result = RUN.sample(SAMPLE)
        self.assertEqual(result["state"], "presence_hint")
        self.assertEqual(result["rf_freshness"], "not_exported")

    def test_missing_readiness_never_becomes_clear_or_presence(self):
        for key in (b"calibrated=1", b"ready=1"):
            with self.assertRaises(ValueError):
                RUN.sample(SAMPLE.replace(key, key[:-1] + b"0"))
        with self.assertRaises(ValueError):
            RUN.sample(SAMPLE.replace(b"training=0", b"training=1"))
        value = SAMPLE.replace(b"calibrated=1", b"calibrated=0").replace(
            b"state=presence_hint", b"state=unknown"
        )
        self.assertEqual(RUN.sample(value)["state"], "unknown")

    def test_rejects_bad_numeric_and_schema(self):
        for value in (
            SAMPLE.replace(b"0.02000000", b"nan"),
            SAMPLE.replace(b"0.02000000", b"inf"),
            SAMPLE + b" calibrated=1",
            SAMPLE.replace(b"advisory=1", b"advisory=0"),
            SAMPLE.replace(b"motion=2", b"motion=9"),
        ):
            with self.assertRaises(ValueError):
                RUN.sample(value)

    def test_report_clock_must_advance(self):
        client = RUN.Client(Console([SAMPLE, SAMPLE]))
        client.read(time.monotonic() + 1)
        with self.assertRaises(ValueError):
            client.read(time.monotonic() + 1)

    def test_exact_ack_before_private_credentials(self):
        console = Console(
            [
                b"PRESENCE_COMMAND_READY force=0",
                b"PRESENCE_OPTIONS seconds=30 force=0 engine=espressif_full amplitude=log",
                b"PRESENCE_CREDENTIALS_READY",
                b"PRESENCE_BEGIN seconds=30 channel=6 calibration=required rf_freshness=not_exported",
            ]
        )
        with patch.object(
            RUN.IDF.RADIO, "write_console", side_effect=lambda c, d: c.write(d)
        ):
            RUN.Client(console).start(b"fixture-network", b"fixture-password", 30)
        self.assertEqual(
            console.writes[-1],
            RUN.IDF.credential_frame(b"fixture-network", b"fixture-password")[0],
        )

    def test_mismatch_and_missing_ack_withhold_credentials(self):
        for ack in (
            b"PRESENCE_OPTIONS seconds=30 force=1 engine=espressif_full amplitude=log",
            None,
        ):
            lines = [b"PRESENCE_COMMAND_READY force=0"]
            if ack:
                lines.append(ack)
            lines.append(b"PRESENCE_CREDENTIALS_READY")
            console = Console(lines)
            with patch.object(
                RUN.IDF.RADIO, "write_console", side_effect=lambda c, d: c.write(d)
            ):
                with self.assertRaises(RuntimeError):
                    RUN.Client(console).start(
                        b"fixture-network", b"fixture-password", 30
                    )
            self.assertEqual(console.writes, [b"PRESENCE 30 log\n"])

    def test_linear_case_requires_matching_amplitude_ack(self):
        console = Console(
            [
                b"PRESENCE_COMMAND_READY force=0",
                b"PRESENCE_OPTIONS seconds=30 force=0 engine=espressif_full amplitude=log",
                b"PRESENCE_CREDENTIALS_READY",
            ]
        )
        with patch.object(
            RUN.IDF.RADIO, "write_console", side_effect=lambda c, d: c.write(d)
        ):
            with self.assertRaises(RuntimeError):
                RUN.Client(console).start(
                    b"fixture-network", b"fixture-password", 30, amplitude="linear"
                )
        self.assertEqual(console.writes, [b"PRESENCE 30 linear\n"])

    def test_reference_fault_does_not_leak_arbitrary_console(self):
        client = RUN.Client(Console([b"PRESENCE_FAIL reason=private-native-detail"]))
        with self.assertRaisesRegex(RuntimeError, "diagnostic withheld"):
            client.line(time.monotonic() + 1)

    def test_unsupported_control_is_not_sent(self):
        console = Console([])
        with self.assertRaises(ValueError):
            RUN.Client(console).command("FORCE_GAIN")
        self.assertEqual(console.writes, [])

    def test_source_digest_is_required_before_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "bad.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("esp-radar/src/bad.c", "untrusted")
            with self.assertRaises(ValueError):
                BUILD.extract_source(package, "esp-radar", Path(tmp) / "out")
            self.assertFalse((Path(tmp) / "out").exists())

    def test_numerical_probe_preserves_failure_counts(self):
        line = b"PRESENCE_PROBE windows=20 bad_input=3 bad_output=2 kernel_errors=2 uninspected=0 wander_calls=16 bad_template=5 bad_current=1 constant_template=0 constant_current=0 last_error=-1 error_cols=13 error_rows0=10 error_rows1=0"
        result = RUN.probe(line)
        self.assertEqual(result["bad_input"], 3)
        self.assertEqual(result["bad_template"], 5)
        self.assertEqual(result["last_error"], -1)
        for invalid in (
            line + b" windows=20",
            line.replace(b"bad_input=3", b"bad_input=nan"),
        ):
            with self.assertRaises(ValueError):
                RUN.probe(invalid)


if __name__ == "__main__":
    unittest.main()
