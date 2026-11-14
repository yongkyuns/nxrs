"""Focused tests for the pinned native configuration model and build CLI."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/host/fixtures/kconfig/Kconfig"
ADAPTER = ROOT / "tools/build/nuttx/kconfig.py"


class NativeModelTest(unittest.TestCase):
    def resolve(self, seed, requests, cli=()):
        with tempfile.TemporaryDirectory() as name:
            stage = Path(name)
            shutil.copyfile(FIXTURE, stage / "Kconfig")
            (stage / ".config").write_text(seed)
            (stage / "requests.json").write_text(json.dumps({
                "catalog": True,
                "requests": {name: [{"value": value, "source": "fixture:1",
                                    "command_line": name in cli}]
                             for name, value in requests.items()},
            }))
            subprocess.run([sys.executable, ADAPTER], cwd=stage, check=True, capture_output=True,
                           env={**os.environ, "NXRS_KCONFIG_REPORT": str(stage / "report.json"),
                                "NXRS_KCONFIG_REQUESTS": str(stage / "requests.json")})
            return json.loads((stage / "report.json").read_text()), (stage / ".config").read_text()

    def test_conditions_choices_and_reverse_dependencies(self):
        report, native = self.resolve(
            "CONFIG_GATE=y\nCONFIG_FEATURE=y\nCONFIG_ALT=y\nCONFIG_SERIAL=y\n"
            "# CONFIG_IMPLIED is not set\nCONFIG_BUFFER=03\n",
            {"BUFFER": "03", "IMPLIED": "n", "SERIAL": "y"},
        )
        self.assertFalse(report["problems"])
        symbols = report["symbols"]
        feature = symbols["FEATURE"]
        self.assertEqual(feature["locations"][0]["menu"], ["Inherited menu"])
        for part in ("CONFIG_GATE", "!CONFIG_BLOCKED", "CONFIG_LIMIT >= 4 || CONFIG_ALT"):
            self.assertIn(part, feature["requires"]["expression"])
        self.assertEqual(symbols["SELECTED"]["visibility"], "n")
        self.assertEqual(symbols["SELECTED"]["effective"], "y")
        self.assertEqual(symbols["SELECTED"]["enabled_by"][0]["symbol"], "FEATURE")
        self.assertIn("CONFIG_SELECTED=y\n", native)
        self.assertEqual(symbols["IMPLIED"]["implied_by"][0]["symbol"], "FEATURE")
        self.assertEqual(symbols["IMPLIED"]["effective"], "n")
        self.assertEqual(symbols["BUFFER"]["ranges"][0]["when"]["value"], "y")
        self.assertEqual(symbols["BUFFER"]["defaults"][0]["when"]["value"], "y")
        self.assertEqual(symbols["SERIAL"]["choice"]["selected"], "SERIAL")
        self.assertEqual(len(symbols["OTHERWISE"]["locations"]), 2)
        self.assertIn("||", symbols["OTHERWISE"]["requires"]["expression"])

    def test_known_disabled_unknown_disabled_and_clamped_values(self):
        report, native = self.resolve("CONFIG_FEATURE=y\n", {
            "NONEXISTENT": "n", "FEATURE": "y", "SELECTED": "n", "LIMIT": '"bad"',
        })
        self.assertNotIn("CONFIG_SELECTED", native)
        problems = "\n".join(report["problems"])
        self.assertIn("CONFIG_NONEXISTENT requested 'n'; unknown symbol", problems)
        self.assertIn("CONFIG_FEATURE requested 'y'; effective 'n'; requires", problems)
        self.assertIn("CONFIG_LIMIT requested '\"bad\"'; invalid int value", problems)
        self.assertNotIn("CONFIG_SELECTED requested", problems)

        report, _ = self.resolve("CONFIG_GATE=y\nCONFIG_FEATURE=y\nCONFIG_BUFFER=9\n",
                                 {"BUFFER": "9"})
        problems = "\n".join(report["problems"])
        self.assertIn("being y-selected", problems)
        self.assertIn("active range 1–4", problems)

    def test_cli_can_replace_a_fragment_choice_and_same_layer_conflicts_fail(self):
        seed = "CONFIG_GATE=y\nCONFIG_LOCAL=y\nCONFIG_SERIAL=y\n"
        report, _ = self.resolve(seed, {"LOCAL": "y", "SERIAL": "y"}, cli=("SERIAL",))
        self.assertEqual(report["symbols"]["LOCAL"]["choice"]["selected"], "SERIAL")
        self.assertFalse(report["problems"])
        report, _ = self.resolve(seed, {"LOCAL": "y", "SERIAL": "y"})
        self.assertTrue(any("CONFIG_LOCAL requested" in problem for problem in report["problems"]))

    def test_prompt_only_conditions_have_navigable_prerequisites(self):
        report, _ = self.resolve("", {})
        setting = report["symbols"]["PROMPT_ONLY"]
        self.assertEqual(setting["requires"], {"expression": "y", "value": "y"})
        self.assertEqual(setting["visibility"], "n")
        self.assertEqual(setting["prerequisites"], ["PROMPT_GATE"])
        self.assertEqual(setting["locations"][0]["prompt_condition"],
                         {"expression": "CONFIG_PROMPT_GATE", "value": "n"})

        report, _ = self.resolve("CONFIG_PROMPT_GATE=y\n", {})
        setting = report["symbols"]["PROMPT_ONLY"]
        self.assertEqual(setting["requires"], {"expression": "y", "value": "y"})
        self.assertEqual(setting["visibility"], "y")
        self.assertEqual(setting["prerequisites"], ["PROMPT_GATE"])


class CoreS3Test(unittest.TestCase):
    @unittest.skipUnless(os.getenv("NXRS_CONFIG_CLI"), "set NXRS_CONFIG_CLI to the built CLI")
    def test_real_debug_and_i2c_examples(self):
        args = [os.environ["NXRS_CONFIG_CLI"], "config", "show", "--app", "event-demo",
                "--platform", "cores3"]
        result = subprocess.run(args, cwd=ROOT, check=True, capture_output=True, text=True)
        report = json.loads(result.stdout)
        symbols = report["symbols"]
        self.assertIn("CONFIG_DEBUG_FEATURES", symbols["DEBUG_ASSERTIONS"]["requires"]["expression"])
        self.assertTrue(any(row["symbol"] == "DEBUG_FEATURES" and row["value"] == "y"
                            for row in symbols["DEBUG_ALERT"]["enabled_by"]))
        self.assertTrue(any(row["symbol"] == "ESP32S3_I2C0" and row["value"] == "y"
                            for row in symbols["I2C"]["enabled_by"]))
        self.assertEqual(symbols["ESP32S3_I2C0_SDAPIN"]["ownership"]["owner"],
                         "resources.sensor_bus.kconfig.ESP32S3_I2C0_SDAPIN")
        choice = subprocess.run(args + ["--kconfig-enable", "ESP32S3_DEFAULT_CPU_FREQ_160"],
                                cwd=ROOT, check=True, capture_output=True, text=True)
        symbols = json.loads(choice.stdout)["symbols"]
        self.assertEqual(symbols["ESP32S3_DEFAULT_CPU_FREQ_160"]["choice"]["selected"],
                         "ESP32S3_DEFAULT_CPU_FREQ_160")


if __name__ == "__main__":
    unittest.main()
