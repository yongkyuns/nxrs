"""Isolation and provenance regressions; synthetic bytes are not firmware evidence."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("progress_build_tests", Path(__file__).with_name("build.py"))
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


class BuildIsolationTests(unittest.TestCase):
    def test_every_source_override_is_rejected(self):
        for key in build.UNSAFE_OVERRIDES:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "competing"):
                build.build_environment(Path("/private"), "select-reserved", {key: "override"})

    def test_each_mode_has_private_sources_cache_and_correct_hook(self):
        directories = set()
        for mode, source_mode in build.MODES.items():
            out = Path("/private") / mode
            env = build.build_environment(out, mode, {"RUSTFLAGS": "unrelated flags"})
            self.assertNotIn("RUSTFLAGS", env)
            self.assertEqual(env["NXRS_CHANNEL_PROGRESS_HOOK_STAGE"], str(build.private.MODES[source_mode]))
            for key in ("NXRS_SHARED_NUTTX_STD_SYSROOT", "NXRS_SHARED_CARGO_TARGET_DIR", "NXRS_CARGO_CONFIG"):
                self.assertTrue(Path(env[key]).is_relative_to(out))
            directories.add(env["NXRS_SHARED_CARGO_TARGET_DIR"])
        self.assertEqual(len(directories), 8)
        self.assertEqual(build.MODES["std-mutex-suite"], "clean")
        self.assertEqual(build.MODES["fifo-channel-suite"], "fifo-ordered")
        with self.assertRaises(ValueError):
            build.build_environment(Path("/private"), "latency", {})

    def test_stage_is_independent_and_leaves_root_lock_unchanged(self):
        root_lock = (build.ROOT / "Cargo.lock").read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = build.stage_workspace(root, build.baseline.resolve_platform("mps2-an521-mock", build.ROOT))
            self.assertEqual(manifest.read_bytes(), (build.HERE / "rust/Cargo.toml").read_bytes())
            self.assertIn('members = ["tests/channel-progress/rust"]', (root / "workspace/Cargo.toml").read_text())
            for name in build.SHARED_FILES:
                staged = root / "workspace/tests/std-channel-perf" / name
                self.assertEqual(staged.read_bytes(), (build.HERE.parent / "std-channel-perf" / name).read_bytes())
                self.assertEqual(staged.stat().st_nlink, 1)
            manifest.write_text("synthetic isolated edit")
            self.assertNotEqual(manifest.read_bytes(), (build.HERE / "rust/Cargo.toml").read_bytes())
        self.assertEqual((build.ROOT / "Cargo.lock").read_bytes(), root_lock)

    def test_staged_workspace_passes_actual_deployment_validation_for_each_platform(self):
        deployment = build.module("progress_deployment_tests", build.ROOT / "tools/check-deployment.py")
        for platform in build.baseline.PLATFORMS:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as temporary:
                profile = build.baseline.resolve_platform(platform, build.ROOT)
                manifest = build.stage_workspace(Path(temporary), profile)
                validated = deployment.validate(manifest, platform, "nuttx")
                self.assertEqual(validated["app_package"], build.PACKAGE)
                self.assertEqual(validated["platform"], platform)

    def test_firmware_uses_only_staged_fixture_abi(self):
        out = Path("/private/image")
        manifest = out / "workspace/tests/channel-progress/rust/Cargo.toml"
        argv = build.firmware_command("mps2-an521-mock", out, manifest)
        self.assertEqual(argv[argv.index("--app-manifest") + 1], manifest)
        self.assertEqual(argv[argv.index("--target-c-source") + 1], out / "workspace/tests/std-channel-perf/native.c")
        self.assertNotIn("--clock-source", argv)


class CompiledSourceTests(unittest.TestCase):
    def fixture(self, root):
        firmware = root / "firmware"
        firmware.mkdir()
        receipt = {"crates": {}}
        messages = []
        for name, pin in build.private.PINS.items():
            relative = f'sources/{name}-{pin["version"]}'
            source = root / "crossbeam" / relative / "src/lib.rs"
            source.parent.mkdir(parents=True)
            source.write_text("// synthetic source identity fixture")
            artifact = firmware / (name + ".rlib")
            artifact.write_bytes(b"synthetic not executable")
            receipt["crates"][name] = {"source_directory": relative}
            messages.append({"reason": "compiler-artifact", "package_id": f'path+synthetic#{name}@{pin["version"]}',
                             "target": {"name": name.replace("-", "_"), "kind": ["lib"], "src_path": str(source)},
                             "filenames": [str(artifact)]})
        return receipt, messages

    def write(self, root, messages):
        (root / "firmware/cargo-messages.jsonl").write_text("\n".join(json.dumps(m) for m in messages) + "\n")

    def test_actual_paths_and_nonempty_compiled_libraries_are_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt, messages = self.fixture(root)
            self.write(root, messages)
            proof = build.cargo_source_proof(root, receipt)
            self.assertEqual(set(proof["sources"]), set(build.private.PINS))
            for source in proof["sources"].values():
                self.assertTrue(source["source_matches"])
                self.assertTrue(source["artifact_sha256"])
            original = messages[0]["target"]["src_path"]
            messages[0]["target"]["src_path"] = messages[1]["target"]["src_path"]
            self.write(root, messages)
            with self.assertRaisesRegex(ValueError, "wrong"):
                build.cargo_source_proof(root, receipt)
            messages[0]["target"]["src_path"] = original
            Path(messages[0]["filenames"][0]).write_bytes(b"")
            self.write(root, messages)
            with self.assertRaisesRegex(ValueError, "missing compiled"):
                build.cargo_source_proof(root, receipt)

    def test_missing_or_duplicate_cargo_source_evidence_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt, messages = self.fixture(root)
            for invalid in (messages[:1], messages + [messages[0]]):
                self.write(root, invalid)
                with self.assertRaisesRegex(ValueError, "expected one"):
                    build.cargo_source_proof(root, receipt)


if __name__ == "__main__":
    unittest.main()
