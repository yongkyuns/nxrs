"""Build provenance controls; synthetic outputs never count as firmware evidence."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "std_channel_build_tests", Path(__file__).with_name("build.py"))
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)

CONFIG = ('CONFIG_BUILD_FLAT=y\n# CONFIG_SMP is not set\n'
          '# CONFIG_DISABLE_PTHREAD is not set\nCONFIG_RR_INTERVAL=10\n'
          'CONFIG_DEBUG_CUSTOMOPT=y\nCONFIG_DEBUG_OPTLEVEL="-O2"\n')
HOOK = {
    "schema": "std-channel-hook-v1", "mode": "latency", "hook_stage": 0,
    "original": {"sha256": "a" * 64}, "selected": {"sha256": "a" * 64},
    "library_tree_sha256": "b" * 64,
    "compiler": {"version": "synthetic test compiler", "rustc_sha256": "c" * 64},
}


class BuildGuards(unittest.TestCase):
    def test_competing_source_compiler_and_encoded_flags_are_rejected(self):
        for variable in build.UNSAFE_OVERRIDES:
            with self.subTest(variable=variable), self.assertRaisesRegex(ValueError, "competing"):
                build.build_environment(Path("/private"), "latency", "2", {variable: "override"})

    def test_modes_cannot_share_build_cache_or_hook_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = set()
            for mode, stage in build.private_std.MODES.items():
                out = root / mode
                env = build.build_environment(out, mode, "2", {})
                self.assertEqual(env["NXRS_STD_CHANNEL_HOOK_STAGE"], str(stage))
                self.assertTrue(Path(env["NXRS_SHARED_NUTTX_STD_SYSROOT"]).is_relative_to(out))
                paths.add(env["NXRS_SHARED_CARGO_TARGET_DIR"])
            self.assertEqual(len(paths), len(build.private_std.MODES))

    def test_single_core_flat_and_requested_optimization_are_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "resolved.config"
            config.write_text(CONFIG)
            values = build.validate_configuration(config, "o2")
            self.assertEqual(values["CONFIG_SMP"], "n")
            for bad in (CONFIG.replace("# CONFIG_SMP is not set", "CONFIG_SMP=y"),
                        CONFIG.replace("CONFIG_BUILD_FLAT=y", "CONFIG_BUILD_KERNEL=y"),
                        CONFIG.replace('"-O2"', '"-Os"')):
                config.write_text(bad)
                with self.assertRaises(ValueError):
                    build.validate_configuration(config, "o2")

    def test_selected_source_boolean_cannot_hide_wrong_path_or_compiler(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            firmware, prepared = root / "firmware", root / "prepared"
            firmware.mkdir()
            expected = prepared / "toolchain" / build.private_std.LIBRARY / "std/src/lib.rs"
            compiler = prepared / "toolchain/bin/rustc"
            proof = {"std_source": str(expected), "rustc": str(compiler),
                     "selected_source_matches": True}
            (firmware / "std-patch.json").write_text(json.dumps({"channel_qualification": HOOK}))
            with patch.object(build.private_std, "validate_receipt", return_value=HOOK):
                for key, value in (("std_source", "/different/std/lib.rs"),
                                   ("rustc", "/different/rustc"),
                                   ("selected_source_matches", False)):
                    (firmware / "std-source.json").write_text(json.dumps({**proof, key: value}))
                    with self.assertRaisesRegex(ValueError, "Cargo did not compile"):
                        build.verify_selected_std(firmware, prepared, "latency")
                (firmware / "std-source.json").write_text(json.dumps(proof))
                (firmware / "std-patch.json").write_text(json.dumps({"channel_qualification": {}}))
                with self.assertRaisesRegex(ValueError, "ledger"):
                    build.verify_selected_std(firmware, prepared, "latency")

    def test_artifact_paths_cannot_escape_build_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "build").mkdir()
            (root / "image").write_bytes(b"outside")
            (root / "build/image").symlink_to(root / "image")
            with self.assertRaisesRegex(ValueError, "escapes"):
                build.relative(root / "build/image", root / "build")

    def test_candidate_profile_is_explicit_and_wrong_targets_are_rejected(self):
        for platform, profile, error in [
            ("pico2-mock", build.simpleboot_digest.PROFILE, "physical ESP32-S3"),
            ("mps2-an521-mock", build.simpleboot_digest.PROFILE, "physical ESP32-S3"),
            ("esp32s3-service-footprint", "invented", "unknown boot image profile"),
        ]:
            with self.subTest(platform=platform, profile=profile), self.assertRaisesRegex(ValueError, error):
                build.build(SimpleNamespace(out=Path("/unused"), platform=platform,
                                            mode="latency", clock_source="baseline",
                                            boot_image_profile=profile))

    def test_native_counter_selection_is_explicit_and_qemu_is_rejected(self):
        env = build.build_environment(Path("/private"), "latency", "2", {}, "native-counter")
        self.assertEqual(env["NXRS_STD_CHANNEL_CLOCK_SOURCE"], "native-counter")
        pico = build.firmware_command("pico2-mock", Path("/private"), "baseline", "native-counter")
        esp = build.firmware_command("esp32s3-service-footprint", Path("/private"), "baseline", "native-counter")
        self.assertIn("ARCH_PERF_EVENTS", pico)
        self.assertNotIn("ARCH_PERF_EVENTS", esp)
        with self.assertRaisesRegex(ValueError, "physical-board profile"):
            build.build(SimpleNamespace(out=Path("/unused"), platform="mps2-an521-mock",
                                        mode="latency", clock_source="native-counter"))


class BuildReceiptTests(unittest.TestCase):
    def run_fixture(self, root, *, fail=False, change_inputs=False,
                    esp=False, digest_candidate=False, fail_candidate=False):
        source = root / "sdk"
        source.mkdir()
        out = root / "out"
        args = SimpleNamespace(out=out, platform=("esp32s3-service-footprint" if esp
                                                  else "mps2-an521-mock"), mode="latency",
                               app_opt_level="2", kernel_optimization="o2", source_sysroot=source,
                               clock_source="baseline", boot_image_profile=(
                                   build.simpleboot_digest.PROFILE if digest_candidate
                                   else "platform-default"))
        profile = {"target": "xtensa-esp32s3-nuttx" if esp else "thumbv8m.main-nuttx-eabi",
                   "image": "nuttx.merged.bin" if esp else "nuttx", "sources": []}

        def prepare(source, prepared, sdk, mode):
            prepared.mkdir()
            (prepared / "std-channel-hook.json").write_text(json.dumps(HOOK))
            (prepared / "std-library-sources.json").write_text("{}")
            return HOOK

        def run(argv, **kwargs):
            if argv[:2] == ["git", "rev-parse"]:
                return "1" * 40
            if argv[:2] == ["git", "status"]:
                return " M tests/std-channel-perf/build.py"
            if fail:
                raise ValueError("synthetic compiler failure")
            firmware = out / "firmware"
            (firmware / "nuttx").mkdir(parents=True)
            (firmware / "nuttx/nuttx").write_bytes(b"synthetic ELF; not execution evidence")
            if esp:
                (firmware / "nuttx/nuttx.merged.bin").write_bytes(b"synthetic original image")
            (firmware / "resolved.config").write_text(CONFIG)
            (firmware / "nuttx-patches.json").write_text('{"synthetic":true}')
            for name in ("provenance.txt", "std-source.json", "std-patch.json", "abi-report.json"):
                (firmware / name).write_text("synthetic regression fixture")
            self.assertEqual(kwargs["env"]["NXRS_STD_CHANNEL_HOOK_STAGE"], "0")
            self.assertEqual(len(kwargs["env"]["NXRS_STD_CHANNEL_BUILD_ID"]), 64)
            return ""

        def make_candidate(firmware, image):
            self.assertTrue(esp and digest_candidate)
            self.assertEqual(image.read_bytes(), b"synthetic original image")
            if fail_candidate:
                raise ValueError("synthetic missing loader proof")
            output = firmware / "nuttx/nuttx.rom-digest.bin"
            report = firmware / "simpleboot-digest.json"
            output.write_bytes(b"synthetic candidate; not firmware")
            report.write_text('{"synthetic":true}')
            return output, report

        inputs = [{"fixture": "a" * 64}, {"fixture": ("b" if change_inputs else "a") * 64}]
        with patch.object(build, "resolve_platform", return_value=profile), \
                patch.object(build, "inventory", side_effect=inputs), \
                patch.object(build, "run", side_effect=run), \
                patch.object(build.private_std, "prepare", side_effect=prepare), \
                patch.object(build, "verify_selected_std", return_value=HOOK), \
                patch.object(build.simpleboot_digest, "prepare", side_effect=make_candidate) as packer, \
                patch.object(build.secrets, "token_hex", return_value="0" * 32), \
                patch.dict(build.os.environ, {}, clear=True):
            if fail or change_inputs or fail_candidate:
                with self.assertRaises(ValueError):
                    build.build(args)
            else:
                build.build(args)
            self.assertEqual(packer.call_count, int(digest_candidate and not fail))
        return out, json.loads((out / "build.json").read_text())

    def test_success_receipt_hashes_actual_artifacts_but_claims_no_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            out, record = self.run_fixture(Path(temporary))
            self.assertEqual(record["status"], "complete")
            self.assertEqual(record["runtime_kind"], "unmeasured")
            self.assertEqual(record["image_sha256"], build.digest(out / record["image_file"]))
            self.assertEqual(record["elf_sha256"], build.digest(out / record["elf_file"]))
            self.assertEqual(record["config_sha256"], build.digest(out / record["config_file"]))
            self.assertEqual(record["std_array_sha256"], record["std_array_original_sha256"])

    def test_candidate_receipt_selects_new_image_without_relabeling_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            out, record = self.run_fixture(Path(temporary), esp=True, digest_candidate=True)
            self.assertEqual(record["status"], "complete")
            self.assertEqual(record["boot_image_profile"], build.simpleboot_digest.PROFILE)
            self.assertEqual(record["runtime_kind"], "unmeasured")
            self.assertTrue(record["image_file"].endswith("nuttx.rom-digest.bin"))
            self.assertEqual((out / record["unmodified_source_image"]).read_bytes(),
                             b"synthetic original image")
            self.assertNotEqual(record["image_sha256"],
                                build.digest(out / record["unmodified_source_image"]))
            for path in [record["image_file"], record["unmodified_source_image"],
                         record["simpleboot_digest_report"], "firmware/nuttx-patches.json"]:
                self.assertEqual(record["artifacts_sha256"][path], build.digest(out / path))

    def test_failed_candidate_does_not_produce_success_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            out, record = self.run_fixture(Path(temporary), esp=True, digest_candidate=True,
                                           fail_candidate=True)
            self.assertEqual(record["status"], "failed")
            self.assertIn("missing loader proof", record["failure"])
            self.assertNotIn("image_file", record)
            self.assertEqual((out / "firmware/nuttx/nuttx.merged.bin").read_bytes(),
                             b"synthetic original image")

    def test_profile_participates_in_build_identity_even_with_same_nonce(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            _, default = self.run_fixture(first, esp=True)
            _, candidate = self.run_fixture(second, esp=True, digest_candidate=True)
            self.assertEqual(default["build_nonce"], candidate["build_nonce"])
            self.assertNotEqual(default["build_id"], candidate["build_id"])
            self.assertNotIn("simpleboot_digest_report", default)

    def test_failed_compile_and_mid_build_source_change_cannot_claim_success(self):
        for case in ("compile", "inputs"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                _, record = self.run_fixture(Path(temporary), fail=case == "compile",
                                              change_inputs=case == "inputs")
                self.assertEqual(record["status"], "failed")
                self.assertTrue(record["failure"])
                self.assertEqual(record["runtime_kind"], "unmeasured")



class ArmUartBenchmarkConfig(unittest.TestCase):
    def test_irq_override_is_explicit_and_arm_only(self):
        expected = ["CMSDK_UART0_TX_IRQ=48", "CMSDK_UART0_RX_IRQ=49"]
        for platform in build.PLATFORMS:
            with self.subTest(platform=platform):
                command = build.firmware_command(platform, Path("/probe"), "baseline")
                configured = [command[index + 1] for index, word in enumerate(command[:-1])
                              if word == "--kconfig-integer"]
                self.assertEqual(configured, expected if platform == "mps2-an521-mock" else [])



if __name__ == "__main__":
    unittest.main()
