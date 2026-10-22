"""CSI build/acceptance rejection controls. No SDK, serial port or RF required."""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "tests/csi-radio"


def load(name):
    spec = importlib.util.spec_from_file_location("csi_" + name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = load("build")
QEMU = load("qemu")
RUN = load("run")


def kernel_report():
    values = {
        "gain_plus6": 10 ** (-6 / 20),
        "gain_minus6": 10 ** (6 / 20),
        "wander": 0.6,
        "motion0": 1,
        "motion1": 2,
        "motion2": 3,
        "motion3": 4,
        "52carrier_first": 1,
        "52carrier_last": 52,
    }
    lines = [
        "CSI_PROBE_ENTERED synthetic_inputs=1",
        "invalid args to get_rx_gain",
        "agc_gain(median): 20, fft_gain@median: 4",
        "CSI_THREADS rounds=100,100 errors=0,0",
        "CSI_REPORT checks=818 failures=0 guards=0 live=0",
    ]
    lines += [f'CSI_VALUE {k} {struct.pack("!f",v).hex()}' for k, v in values.items()]
    lines += [
        f"CSI_FAULT allocation={i} result=257 live=0 guards=0" for i in range(1, 5)
    ]
    return "\n".join(lines)


class CsiRadioTests(unittest.TestCase):
    def test_bad_target_port_withholds_credentials(self):
        args = SimpleNamespace(
            port="unused",
            host_ip="192.0.2.1",
            bssid="associated",
            seconds=30,
            capture=None,
            force_baseline=False,
            transport_only=True,
            ack_spare_board=False,
            ack_camera_maintenance=True,
        )
        console = MagicMock()
        console.__enter__.return_value = console
        console.write.side_effect = len
        console.read.side_effect = [
            b"nsh> ",
            b"CSI_TEST_OPTIONS transport_only=1 raw=0 force=0 peer_port=5432\nNXRS_WIFI_CREDENTIALS_READY",
        ]
        echo = MagicMock()
        echo.__enter__.return_value = echo
        echo.getsockname.return_value = ("192.0.2.1", 54321)
        worker = MagicMock()
        worker.is_alive.return_value = False
        with patch.dict(
            sys.modules, {"serial": SimpleNamespace(Serial=lambda: console)}
        ), patch.object(RUN.socket, "socket", return_value=echo), patch.object(
            RUN.threading, "Thread", return_value=worker
        ), patch.object(
            RUN.time, "sleep"
        ):
            with self.assertRaisesRegex(RuntimeError, "target options/UDP port differ"):
                RUN.qualify(args, b"unused-ssid", b"private-passphrase")
        wire = b"".join(x.args[0] for x in console.write.call_args_list)
        self.assertEqual(
            wire,
            b"\n\n\ncsi_radio --maintenance-camera 30 associated 192.0.2.1:54321 --transport-only\n",
        )
        worker.join.assert_called_once_with(timeout=2)

    def test_console_transfer_preserves_long_frames_and_rejects_short_writes(self):
        console = MagicMock()
        console.write.side_effect = len
        payload = bytes(range(100))
        with patch.object(RUN.time, "sleep"):
            RUN.write_console(console, payload)
        chunks = [x.args[0] for x in console.write.call_args_list]
        self.assertEqual(b"".join(chunks), payload)
        self.assertTrue(all(0 < len(x) <= 16 for x in chunks))
        console.write.side_effect = lambda _: 0
        with self.assertRaises(RuntimeError):
            RUN.write_console(console, payload)

    def test_console_capacity_and_options_reject_truncated_commands(self):
        good = "CONFIG_LINE_MAX=256\nCONFIG_NSH_MAXARGUMENTS=12\n"
        self.assertEqual(BUILD.check_console_config(good)["line_max"], 256)
        for bad in (good.replace("=256", "=64"), good.replace("=12", "=7"), ""):
            with self.assertRaises(ValueError):
                BUILD.check_console_config(bad)
        ack = b"CSI_TEST_OPTIONS transport_only=1 raw=0 force=0 peer_port=54321\n"
        RUN.validate_options(ack, True, False, False, 54321)
        for bad in (
            ack.replace(b"=54321", b"=5432"),
            ack.replace(b"transport_only=1", b"transport_only=0"),
            b"",
            ack + ack,
        ):
            with self.assertRaises(RuntimeError):
                RUN.validate_options(bad, True, False, False, 54321)

    def test_integer_build_overrides_reject_untrusted_assignments(self):
        for value in (
            "LINE_MAX=-1",
            "line_max=256",
            "LINE_MAX=256;touch unused",
            "LINE_MAX=abc",
            "LINE_MAX=",
        ):
            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / "tools/build-nuttx-std-app.sh"),
                    "--kconfig-set",
                    value,
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Invalid integer Kconfig assignment", result.stderr)

    def test_transport_control_never_qualifies_csi_capture(self):
        good = (
            b"CSI_TRANSPORT_RESULT echoes=10 csi_configured=0\n"
            b"CSI_TRANSPORT_PASS cleanup=confirmed station=disconnected worker=joined advisory_only=1\n"
        )
        result = RUN.validate_report(good, set(range(10)), False, True)
        self.assertFalse(result["csi_configured"])
        for data, served, force in (
            (good, set(range(9)), False),
            (good, set(range(10)), True),
            (
                good.replace(b"csi_configured=0", b"csi_configured=1"),
                set(range(10)),
                False,
            ),
            (good.replace(b"worker=joined", b"worker=unknown"), set(range(10)), False),
            (good + good, set(range(10)), False),
        ):
            with self.assertRaises(ValueError):
                RUN.validate_report(data, served, force, True)
        with self.assertRaises(ValueError):
            RUN.validate_report(good, set(range(10)), False)

    def test_transport_control_cannot_request_rf_operations(self):
        args = SimpleNamespace(
            ack_spare_board=False,
            ack_camera_maintenance=True,
            force_baseline=False,
            transport_only=True,
            capture=None,
        )
        self.assertEqual(RUN.capture_mode(args), "--maintenance-camera")
        args.capture = Path("unused")
        with self.assertRaises(ValueError):
            RUN.capture_mode(args)
        args.capture = None
        args.force_baseline = True
        with self.assertRaises(ValueError):
            RUN.capture_mode(args)

    def test_failure_diagnostics_do_not_export_arbitrary_console(self):
        self.assertEqual(
            RUN.failure_detail(b"CSI_RADIO_FAIL error=Protocol\r\n"), "Protocol"
        )
        self.assertEqual(
            RUN.failure_detail(
                b"CSI_CAPTURE_REJECT reason=rf_timeout frames=0\nCSI_RADIO_FAIL error=Timeout\n"
            ),
            "Timeout; capture=rf_timeout",
        )
        self.assertEqual(
            RUN.failure_detail(b"CSI_SDK_ERROR status=259\nCSI_RADIO_FAIL error=Io\n"),
            "Io; SDK status=259",
        )
        for data in (
            b"private console",
            b"CSI_RADIO_FAIL error=private",
            b"CSI_RADIO_FAIL error=Io private",
        ):
            self.assertEqual(RUN.failure_detail(data), "unclassified")
        self.assertIsNotNone(
            RUN.DIAGNOSTIC.fullmatch(
                b"CSI_CAPTURE_REJECT reason=queue overflow=1 malformed=0 callback_dropped=0"
            )
        )
        self.assertIsNone(RUN.DIAGNOSTIC.fullmatch(b"CSI_SDK_ERROR status=259 private"))
        wifi = b"NXRS_WIFI_STAGE_FAIL stage=connect error=Timeout"
        self.assertIn("stage=connect error=Timeout", RUN.failure_detail(wifi))
        self.assertEqual(RUN.failure_detail(wifi + b" private"), "unclassified")

    def test_association_failure_survives_stage_drain_and_credentials_stay_private(
        self,
    ):
        args = SimpleNamespace(
            port="unused",
            host_ip="192.0.2.1",
            bssid="associated",
            seconds=30,
            capture=None,
            force_baseline=False,
            ack_spare_board=False,
            ack_camera_maintenance=True,
        )
        failure = b"NXRS_WIFI_STAGE_FAIL stage=connect error=Timeout\r\nCSI_RADIO_FAIL error=Timeout\r\n"
        for payload, expected in (
            (
                failure,
                "stage=connect error=Timeout.*host_stage=association.*echoes_served=0",
            ),
            (b"private-passphrase\n" + failure, "credential echo detected"),
        ):
            console = MagicMock()
            console.__enter__.return_value = console
            console.write.side_effect = len
            console.read.side_effect = [
                b"nsh> ",
                b"CSI_TEST_OPTIONS transport_only=0 raw=0 force=0 peer_port=12345\nNXRS_WIFI_CREDENTIALS_READY",
                payload,
            ]
            echo = MagicMock()
            echo.__enter__.return_value = echo
            echo.getsockname.return_value = ("192.0.2.1", 12345)
            worker = MagicMock()
            worker.is_alive.return_value = False
            with patch.dict(
                sys.modules, {"serial": SimpleNamespace(Serial=lambda: console)}
            ), patch.object(RUN.socket, "socket", return_value=echo), patch.object(
                RUN.threading, "Thread", return_value=worker
            ):
                with self.assertRaisesRegex(RuntimeError, expected) as error:
                    RUN.qualify(args, b"unused-ssid", b"private-passphrase")
                self.assertNotIn("private-passphrase", str(error.exception))
                worker.join.assert_called_once_with(timeout=2)

    def test_maintenance_is_explicit_and_cannot_force_gain(self):
        def mode(spare=False, maintenance=False, force=False):
            return RUN.capture_mode(
                SimpleNamespace(
                    ack_spare_board=spare,
                    ack_camera_maintenance=maintenance,
                    force_baseline=force,
                )
            )

        self.assertEqual(mode(spare=True), "--spare-board")
        self.assertEqual(mode(maintenance=True), "--maintenance-camera")
        self.assertEqual(mode(spare=True, force=True), "--spare-board")
        for args in (
            {},
            dict(spare=True, maintenance=True),
            dict(maintenance=True, force=True),
        ):
            with self.assertRaises(ValueError):
                mode(**args)

    def test_qemu_requires_all_evidence_and_independent_numerics(self):
        good = kernel_report()
        QEMU.verify(good, "CSI_NSH_STATUS_0\n")
        for old, new in [
            ("checks=818", "checks=817"),
            ("failures=0", "failures=1"),
            ("guards=0", "guards=1"),
            ("live=0", "live=1"),
            ("rounds=100,100", "rounds=100,99"),
            ("result=257", "result=0"),
            ("CSI_VALUE wander 3f19999a", "CSI_VALUE wander 00000000"),
            ("CSI_PROBE_ENTERED", "MISSING"),
        ]:
            with self.subTest(field=old), self.assertRaises(ValueError):
                QEMU.verify(good.replace(old, new), "CSI_NSH_STATUS_0\n")
        for bad in ["", good + "\n" + good, good + "\nPANIC"]:
            with self.assertRaises(ValueError):
                QEMU.verify(bad, "CSI_NSH_STATUS_0\n")
        with self.assertRaises(ValueError):
            QEMU.verify(good, "CSI_NSH_STATUS_1\n")

    def test_qemu_failure_control_cannot_report_success(self):
        QEMU.verify(
            "CSI_PROBE_ENTERED\nCSI_DELIBERATE_FAILURE", "CSI_NSH_STATUS_1\n", True
        )
        with self.assertRaises(ValueError):
            QEMU.verify(kernel_report(), "CSI_NSH_STATUS_1\n", True)

    def test_radio_pass_requires_rf_transport_and_cleanup(self):
        good = (
            b"CSI_CAPTURE_RESULT frames=50 factors=1 echoes=10 raw_exported=0 force_exercised=0 loss=0 advisory_only=1\n"
            b"CSI_RADIO_PASS cleanup=confirmed station=disconnected worker=joined advisory_only=1\n"
        )
        report = RUN.validate_report(good, set(range(10)), False)
        self.assertEqual(report["gain_metadata_abi"], "unverified")
        for old, new in [
            (b"frames=50", b"frames=49"),
            (b"echoes=10", b"echoes=2000000000"),
            (b"factors=1", b"factors=0"),
            (b"loss=0", b"loss=1"),
            (b"cleanup=confirmed", b"cleanup=failed"),
            (b"force_exercised=0", b"force_exercised=1"),
        ]:
            with self.assertRaises(ValueError):
                RUN.validate_report(good.replace(old, new), set(range(10)), False)
        for evidence, served in [
            (good + good, set(range(10))),
            (good + b"CSI_RADIO_FAIL", set(range(10))),
            (good, set(range(9))),
            (b"", set()),
        ]:
            with self.assertRaises(ValueError):
                RUN.validate_report(evidence, served, False)

    def test_kernel_output_is_confined_to_fixture_namespace(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            BUILD, "ROOT", Path(tmp)
        ):
            target = Path(tmp) / "target"
            target.mkdir()
            self.assertEqual(
                BUILD.validate_output(target / "csi-radio-test"),
                (target / "csi-radio-test").resolve(),
            )
            for path in [
                tmp,
                target,
                target / "camera-stream-device",
                target / "csi-radio-test" / "nested",
            ]:
                with self.assertRaises(ValueError):
                    BUILD.validate_output(path)

    def test_component_archives_are_pinned_and_extraction_is_allowlisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            name = "esp_csi_gain_ctrl"
            package = path / "package.zip"
            payload = b"fake Xtensa archive"
            with zipfile.ZipFile(package, "w") as z:
                z.writestr(name + "/6.0/esp32s3/lib" + name + ".a", payload)
                z.writestr(name + "/include/" + name + ".h", "header")
                z.writestr(name + "/LICENSE", "license")
                z.writestr("../../escape", "bad")
            pins = copy.deepcopy(BUILD.PINS)
            pins["packages"][name]["zip_sha256"] = hashlib.sha256(
                package.read_bytes()
            ).hexdigest()
            pins["packages"][name]["archive_sha256"] = hashlib.sha256(
                payload
            ).hexdigest()
            with patch.object(BUILD, "PINS", pins):
                out = BUILD.stage_package(name, package, path / "vendor")
                self.assertEqual(out.read_bytes(), payload)
                self.assertFalse((path / "escape").exists())
                pins["packages"][name]["archive_sha256"] = "0" * 64
                with self.assertRaisesRegex(ValueError, "archive SHA256"):
                    BUILD.stage_package(name, package, path / "other")
            with self.assertRaisesRegex(ValueError, "package SHA256"):
                BUILD.stage_package(name, package, path / "nope")

    @unittest.skipUnless(
        hasattr(os, "O_NOFOLLOW"), "POSIX hardware-runner file protection"
    )
    def test_private_capture_is_exclusive_and_never_inside_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private.log"
            with RUN.private_capture(path) as capture:
                capture.write(b"test")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                RUN.private_capture(path)
        with self.assertRaises(ValueError):
            RUN.private_capture(ROOT / "target/private-radio.log")

    def test_pins_match_checked_in_dependencies(self):
        import subprocess

        for component, key in [("nuttx", "nuttx"), ("nuttx-apps", "nuttx_apps")]:
            actual = subprocess.check_output(
                ["git", "rev-parse", f":external/{component}"], cwd=ROOT, text=True
            ).strip()
            self.assertEqual(actual, BUILD.PINS[key])


if __name__ == "__main__":
    unittest.main()
