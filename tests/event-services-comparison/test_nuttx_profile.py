"""Regressions for the isolated minimal NuttX profile; no SDK tools required."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "event_services_nuttx_profile_tests", HERE / "nuttx_profile.py")
profile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profile)


PROTECTED = {
    "BUILD_FLAT": "y",
    "SMP": "n",
    "USEC_PER_TICK": "10000",
    "RR_INTERVAL": "10",
    "SYSTEM_TIME64": "y",
    "FS_LARGEFILE": "y",
    "TLS_GLOBAL_KEYS": "y",
    "TLS_NELEM": "4",
    "TLS_DTOR_ITERATIONS": "4",
    "TLS_NCLEANUP": "4",
    "PTHREAD_STACK_DEFAULT": "2048",
    "ARCH_INTERRUPTSTACK": "2048",
    "IDLETHREAD_STACKSIZE": "2048",
    "MQ_MAXMSGSIZE": "128",
    "PREALLOC_MQ_MSGS": "16",
    "PREALLOC_MQ_IRQ_MSGS": "8",
    "STACK_COLORATION": "y",
    "DEBUG_FEATURES": "y",
    "DEBUG_ASSERTIONS": "y",
    "USERLED_LOWER_READSTATE": "y",
    "ESP32S3_DEFAULT_CPU_FREQ_MHZ": "240",
    "ESPRESSIF_FLASH_MODE_DIO": "y",
    "ESPRESSIF_FLASH_FREQ_40M": "y",
    "ESP32S3_INSTRUCTION_CACHE_SIZE": "16384",
    "ESP32S3_DATA_CACHE_SIZE": "16384",
    "ESP32S3_USBSERIAL": "y",
}


def write_config(path, settings):
    path.write_text("".join(f"CONFIG_{name}={value}\n"
                             for name, value in sorted(settings.items())))


class MinimalProfileValidationTests(unittest.TestCase):
    def test_disabled_baseline_settings_are_not_lost_when_resolving_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / 'resolved.config'
            config.write_text('# CONFIG_ARCH_LEDS is not set\nCONFIG_USE=y\n')
            self.assertEqual(profile.values(config), {'CONFIG_ARCH_LEDS': 'n', 'CONFIG_USE': 'y'})

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.baseline = self.root / "baseline.config"
        self.resolved = self.root / "resolved.config"
        self.overlay = profile.values(HERE / "nuttx-minimal.conf")
        self.baseline_settings = dict(PROTECTED)
        self.baseline_settings.update({
            "SCHED_HPWORKSTACKSIZE": "4096",
            "SCHED_LPWORKSTACKSIZE": "4096",
        })
        self.resolved_settings = dict(self.baseline_settings)
        self.resolved_settings.update(
            {name.removeprefix("CONFIG_"): value
             for name, value in self.overlay.items()})

    def validate(self):
        write_config(self.baseline, self.baseline_settings)
        write_config(self.resolved, self.resolved_settings)
        profile.validate(self.resolved, self.baseline)

    def test_matched_timing_abi_resource_assertion_and_cpu_settings_cannot_drift(self):
        for name in PROTECTED:
            with self.subTest(setting=name):
                self.resolved_settings = dict(self.baseline_settings)
                self.resolved_settings.update(
                    {key.removeprefix("CONFIG_"): value
                     for key, value in self.overlay.items()})
                self.resolved_settings[name] = "n" if PROTECTED[name] != "n" else "y"
                write_config(self.baseline, self.baseline_settings)
                write_config(self.resolved, self.resolved_settings)
                with self.assertRaisesRegex(ValueError, "changed a matched setting"):
                    profile.validate(self.resolved, self.baseline)

    def test_disabled_overlay_symbols_may_be_absent_after_resolution(self):
        for symbol, value in self.overlay.items():
            if value == "n":
                self.resolved_settings.pop(symbol.removeprefix("CONFIG_"), None)
        self.validate()

    def test_required_posix_interfaces_cannot_be_disabled(self):
        for name in ("DISABLE_PTHREAD", "DISABLE_MQUEUE", "DISABLE_POLL"):
            with self.subTest(setting=name):
                self.resolved_settings = dict(self.baseline_settings)
                self.resolved_settings.update(
                    {key.removeprefix("CONFIG_"): value
                     for key, value in self.overlay.items()})
                self.resolved_settings[name] = "y"
                write_config(self.baseline, self.baseline_settings)
                write_config(self.resolved, self.resolved_settings)
                with self.assertRaisesRegex(ValueError, "removed required POSIX support"):
                    profile.validate(self.resolved, self.baseline)

    def test_overlay_removes_shell_and_psram_without_changing_worker_or_queue_caps(self):
        for name in ("NSH_LIBRARY", "SYSTEM_NSH", "ESP32S3_SPIRAM"):
            self.assertEqual(self.overlay["CONFIG_" + name], "n")
        for name in ("SCHED_HPWORKSTACKSIZE", "SCHED_LPWORKSTACKSIZE",
                     "MQ_MAXMSGSIZE", "PREALLOC_MQ_MSGS", "PREALLOC_MQ_IRQ_MSGS"):
            self.assertNotIn("CONFIG_" + name, self.overlay)
            self.assertEqual(self.resolved_settings[name], self.baseline_settings[name])
        self.validate()


class FrozenRustInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture_here = self.root / "event-services-comparison"
        self.bundle = self.root / "bundle"
        self.tree = self.root / "tree"
        (self.bundle).mkdir()
        (self.tree).mkdir()
        (self.root / "upstream/rust-llvm").mkdir(parents=True)
        (self.root / "tests/nuttx-std").mkdir(parents=True)
        self.fixture_here.mkdir()
        (self.root / "upstream/rust-llvm/upstream.json").write_text(
            json.dumps({"rust_revision": "rust-pin", "revision": "llvm-pin"}))
        (self.root / "tests/nuttx-std/link.py").write_text("frozen link wrapper\n")
        self.elf = self.bundle / "rust-input.elf"
        self.elf.write_bytes(b"mock relocatable elf")
        target = self.tree / "xtensa-esp32s3-nuttx.json"
        target.write_text('{"arch":"xtensa"}\n')
        self.source_names = ("core.rs", "nuttx.rs", "controls.rs")
        source_hashes = {}
        for name in self.source_names:
            source = self.fixture_here / name
            source.write_text(f"// frozen {name}\n")
            source_hashes[name] = profile.helpers.digest(source)
        self.proof = {
            "rust_revision": "rust-pin",
            "llvm_revision": "llvm-pin",
            "elf_sha256": profile.helpers.digest(self.elf),
            "target_spec_sha256": profile.helpers.digest(target),
            "link_wrapper_sha256": profile.helpers.digest(
                self.root / "tests/nuttx-std/link.py"),
            "source_sha256": source_hashes,
            "app_opt_level": "2",
            "std_build_features": [
                "backtrace-trace-only", "optimize_for_size", "panic_immediate_abort"],
        }
        self.proof_path = self.bundle / "compiler-input.json"
        self.write_proof()

    def write_proof(self):
        self.proof_path.write_text(json.dumps(self.proof))

    def verify(self):
        with patch.object(profile, "ROOT", self.root), \
             patch.object(profile, "HERE", self.fixture_here), \
             patch.object(profile.subprocess, "check_output",
                          return_value="Type: REL (Relocatable file)\nMachine: Xtensa\n") as check:
            result = profile.verify_rust_input(self.bundle, self.tree)
        check.assert_called_once_with(
            ["xtensa-esp32s3-elf-readelf", "-h", self.elf], text=True)
        return result

    def test_valid_frozen_bundle_is_accepted_using_mocked_readelf(self):
        self.assertEqual(self.verify(), self.proof)

    def test_invalid_elf_hash_is_rejected(self):
        self.proof["elf_sha256"] = hashlib.sha256(b"different image").hexdigest()
        self.write_proof()
        with self.assertRaisesRegex(ValueError, "input hash differs"):
            self.verify()

    def test_missing_firmware_source_hash_is_rejected(self):
        del self.proof["source_sha256"]["controls.rs"]
        self.write_proof()
        with self.assertRaisesRegex(ValueError, "lacks firmware source identities"):
            self.verify()

    def test_changed_firmware_source_is_rejected(self):
        (self.fixture_here / "nuttx.rs").write_text("// changed firmware source\n")
        with self.assertRaisesRegex(ValueError, "firmware input changed: nuttx.rs"):
            self.verify()

    def test_changed_target_specification_is_rejected(self):
        (self.tree / "xtensa-esp32s3-nuttx.json").write_text('{"arch":"other"}\n')
        with self.assertRaisesRegex(ValueError, "target specification differs"):
            self.verify()

    def test_changed_optimization_policy_is_rejected(self):
        self.proof["app_opt_level"] = "3"
        self.write_proof()
        with self.assertRaisesRegex(ValueError, "optimization policy differs"):
            self.verify()

    def test_changed_standard_library_optimization_features_are_rejected(self):
        self.proof["std_build_features"] = ["optimize_for_size"]
        self.write_proof()
        with self.assertRaisesRegex(ValueError, "optimization policy differs"):
            self.verify()


class ConsoleInputTests(unittest.TestCase):
    def test_command_buffer_bounds_input_and_reports_overflow(self):
        source = (HERE / "nuttx_console.c").read_text()
        declaration = re.search(r"char\s+command\s*\[\s*(\d+)\s*\]", source)
        self.assertIsNotNone(declaration)
        self.assertGreater(int(declaration.group(1)), 1)
        self.assertRegex(source, r"length\s*<\s*sizeof\(command\)\s*-\s*1")
        self.assertIn("overflow = 1;", source)
        self.assertIn("if (overflow)", source)


if __name__ == "__main__":
    unittest.main()
