"""Pure target-witness gates; no emulator, SDK, device or camera is opened."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUN = load("csi_policy_probe_run", "tests/csi-lab/target_probe_run.py")
BUILD = load("csi_policy_probe_build", "tests/csi-lab/target_probe.py")
ARITH = load("csi_policy_arithmetic", "tests/csi-lab/arithmetic.py")
FIXTURE = load("csi_policy_fixture", "tests/csi-lab/target_probe_fixture.py")


class PolicyProbeTests(unittest.TestCase):
    def test_actual_fused_shape_is_rejected_without_weakening_product_witness(self):
        unfused = "\n".join("0000: mul.s f0, f1, f2" for _ in range(4))
        self.assertEqual(ARITH.qualify(unfused)["fused_accumulations"], 0)
        for assembly in (
            "",
            unfused.replace("mul.s", "madd.s"),
            unfused.replace("mul.s", "msub.s"),
            unfused + "\n0000: madd.s f14, f13, f12",
        ):
            with self.subTest(case=assembly):
                with self.assertRaises(ValueError):
                    ARITH.qualify(assembly)

    def test_synthetic_fixture_cases_are_explicit_bounded_and_distinct(self):
        blobs = [FIXTURE.generate(name) for name in FIXTURE.CASES]
        self.assertEqual(len(set(blobs)), 6)
        self.assertTrue(
            all(
                blob.startswith(b"CSIMP001") and len(blob) < 1024 * 1024
                for blob in blobs
            )
        )
        for name, identifier in (
            ("physical", 1),
            ("motion-cycle", 0),
            ("motion-cycle", 17),
        ):
            with self.assertRaises(ValueError):
                FIXTURE.generate(name, identifier)

    def test_uart_newlines_only_are_normalized_and_extra_policy_output_is_rejected(
        self,
    ):
        block = (
            RUN.BEGIN
            + b"\nCSI_POLICY_CHECK case=00000001 time=0000000000000000 bits="
            + b"0" * 256
            + b"\nCSI_POLICY_END cases=1 events=1 checkpoints=1 distances=0 physical_detector_admitted=0\n"
        )
        self.assertEqual(
            RUN.transcript(b"boot diagnostic\r\n" + block.replace(b"\n", b"\r\n")),
            block,
        )
        for value in (
            block + block,
            block.split(b"CSI_POLICY_END")[0],
            block.replace(b"CSI_POLICY_CHECK", b"panic"),
            block + b"CSI_POLICY_FAIL\n",
            RUN.BEGIN + b"\n" + block,
            block[:-1],
            block.replace(b"bits=", b"bits=\r"),
        ):
            with self.subTest(case=value[:32]):
                with self.assertRaises(ValueError):
                    RUN.transcript(value)

        self.assertEqual(RUN.complete_lines(b"CSI_POLICY_END cases=1"), [])
        self.assertEqual(RUN.complete_lines(b"CSI_POLICY_FAIL reason=partial"), [])
        self.assertEqual(
            RUN.complete_lines(b"CSI_POLICY_END cases=1\r\n"),
            [b"CSI_POLICY_END cases=1"],
        )

    def test_probe_staging_keeps_normal_entry_intact_and_selects_no_radio_uart(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            fixture = base / "fixture"
            fixture.joinpath("src").mkdir(parents=True)
            source = '#[path = "../../idf60/src/lib.rs"]\nmod sdk;\n#[no_mangle]\npub extern "C" fn presence_main() { initialize_radio_and_usb(); }\n'
            fixture.joinpath("src/lib.rs").write_text(source)
            fixture.joinpath("main").mkdir()
            fixture.joinpath("main/sdk.c").write_text("/* native witness */\n")
            fixture.joinpath("sdkconfig.defaults").write_text(
                "CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y\nCONFIG_SPIRAM=y\nCONFIG_SPIRAM_MEMTEST=y\n"
            )
            data = base / "input.bin"
            data.write_bytes(b"CSIMP001")
            data.chmod(0o600)
            with patch.object(BUILD, "PRESENCE", fixture):
                BUILD.stage(base / "probe", data)
            self.assertEqual(fixture.joinpath("src/lib.rs").read_text(), source)
            entry = base.joinpath("probe/src/lib.rs").read_text()
            self.assertNotIn("initialize_radio_and_usb", entry)
            self.assertIn("policy_probe::run", entry)
            self.assertIn("include_bytes!", entry)
            config = base.joinpath("probe/sdkconfig.defaults").read_text()
            self.assertIn("CONFIG_ESP_CONSOLE_UART_DEFAULT=y", config)
            self.assertIn("CONFIG_SPIRAM=n", config)
            self.assertNotIn("CONFIG_SPIRAM_MEMTEST", config)
            with self.assertRaises(ValueError):
                BUILD.stage(base / "probe", data)
            data.chmod(0o644)
            with self.assertRaises(ValueError):
                BUILD.stage(base / "other", data)


if __name__ == "__main__":
    unittest.main()
