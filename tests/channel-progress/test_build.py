"""Isolation and provenance regressions; synthetic bytes are not firmware evidence."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("progress_build_tests", Path(__file__).with_name("build.py"))
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


class BuildIsolationTests(unittest.TestCase):
    def test_invalid_boot_profile_refuses_before_build_work_or_output_creation(self):
        for platform, profile in (("mps2-an521-mock", build.boot_image.PROFILE),
                                  ("esp32s3-service-footprint", "unknown"),
                                  ("esp32s3-service-footprint", None)):
            with self.subTest(platform=platform, profile=profile), tempfile.TemporaryDirectory() as temporary:
                out = Path(temporary) / "must-not-exist"
                with mock.patch.object(build.baseline, "resolve_platform") as resolve:
                    with self.assertRaises(ValueError):
                        build.build(SimpleNamespace(out=out, mode="select-reserved", platform=platform,
                                                    boot_image_profile=profile))
                resolve.assert_not_called()
                self.assertFalse(out.exists())

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


class BootProfileIdentityTests(unittest.TestCase):
    # These are the original identities of the four retained legacy ESP images.
    # Reading their receipts exercises actual historical inputs, not new
    # fixtures regenerated under the current identity rule.
    HISTORICAL = (
        ("crossbeam/select-reserved", "5a1e3ed022968efb6321df835cdf969e1dfe5d549b5f0a7e1c9c1b198b9bfe6c"),
        ("crossbeam/select-published", "8817b6d0e3f02a5b1fb79bf1ad548fe60d48cf700691732d601f787f4aa9bfd7"),
        ("std/mutex-admit-held", "53304ab7f810b613bd4136ba98c1e6af96e54cc592366033808198b85a3f838c"),
        ("fifo/fifo-try-recv-reserved", "133d1de73dd00976231646546c70ecdcff21e150cfa6686d1247ac10ae9b4f6d"),
    )
    ORIGINAL_FIELDS = (
        "mode", "hook_stage", "platform", "target", "sdk", "source_sha256",
        "kernel_revision", "apps_revision", "build_nonce", "crossbeam_receipt",
        "compiler_identity", "std_library_tree_sha256", "staged_inputs_sha256",
    )

    def records(self):
        root = build.HERE / "results/2026-11-04-esp32s3/runs"
        for relative, identity in self.HISTORICAL:
            yield json.loads((root / relative / "run/build.json").read_text()), identity

    def test_legacy_crossbeam_std_and_fifo_identity_contracts_remain_exact(self):
        import test_analyze
        analyze = test_analyze.analyze
        self.assertEqual(build.IDENTITY_FIELDS, self.ORIGINAL_FIELDS)
        self.assertEqual(analyze.IDENTITY_FIELDS, self.ORIGINAL_FIELDS)
        for record, identity in self.records():
            with self.subTest(mode=record["mode"]):
                self.assertNotIn("boot_image_profile", record)
                expected = self.ORIGINAL_FIELDS + (("fifo_kernel_source_sha256",)
                                                   if record["mode"] == "fifo-channel-suite" else ())
                self.assertEqual(build.identity_fields(record["mode"]), expected)
                self.assertEqual(build.identity_fields(record["mode"], record), expected)
                self.assertEqual(record["build_id"], identity)
                self.assertEqual(build.canonical({key: record[key] for key in expected}), identity)
                modes = (analyze.STD_TRANSFER_MODES if record["mode"] == "std-mutex-suite"
                         else analyze.FIFO_MODES if record["mode"] == "fifo-channel-suite"
                         else (record["mode"],))
                for mode in (record["mode"], *modes):
                    self.assertEqual(analyze.identity_fields(mode, record), expected)
                    self.assertEqual(analyze.canonical({key: record[key] for key in
                                                       analyze.identity_fields(mode, record)}), identity)

    def test_explicit_default_and_candidate_profiles_bind_distinct_runtime_identities(self):
        import test_analyze
        analyze = test_analyze.analyze
        for record, legacy_identity in self.records():
            with self.subTest(mode=record["mode"]):
                identities = {legacy_identity}
                for profile in (build.boot_image.DEFAULT, build.boot_image.PROFILE):
                    record["boot_image_profile"] = profile
                    fields = build.identity_fields(record["mode"], record)
                    self.assertEqual(fields[-1], "boot_image_profile")
                    self.assertEqual(analyze.identity_fields(record["mode"], record), fields)
                    identities.add(build.canonical({key: record[key] for key in fields}))
                self.assertEqual(len(identities), 3)


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
