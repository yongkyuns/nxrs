"""Build-contract controls. Synthetic artifacts below are never execution evidence."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "event_path_build_tests", Path(__file__).with_name("build.py"))
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)

CONFIG = ('CONFIG_BUILD_FLAT=y\n# CONFIG_SMP is not set\n'
          '# CONFIG_DISABLE_PTHREAD is not set\nCONFIG_RR_INTERVAL=10\n'
          'CONFIG_DEBUG_CUSTOMOPT=y\nCONFIG_DEBUG_OPTLEVEL="-O2"\n')
HOOK = {
    "schema": "std-channel-hook-v1", "mode": "latency", "hook_stage": 0,
    "channel_instrumented": False,
    "original": {"sha256": "a" * 64}, "selected": {"sha256": "a" * 64},
    "library_tree_sha256": "b" * 64,
    "compiler": {"version": "synthetic test compiler", "rustc_sha256": "c" * 64},
}


def registry_fixture(root, name="crossbeam-channel", version="0.5.17"):
    directory = f"{name}-{version}"
    source = root / "registry/src/test-index" / directory
    source.mkdir(parents=True)
    files = {"Cargo.toml": f'[package]\nname="{name}"\nversion="{version}"\n'.encode(),
             "src/lib.rs": b"// synthetic registry source for a negative control\n"}
    archive = root / "registry/cache/test-index" / f"{directory}.crate"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz") as contents:
        for relative, payload in files.items():
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            member = tarfile.TarInfo(f"{directory}/{relative}")
            member.size = len(payload)
            contents.addfile(member, io.BytesIO(payload))
    (source / ".cargo-ok").write_text('{"v":1}')
    locked = {"name": name, "version": version, "source": build.REGISTRY,
              "checksum": build.digest(archive)}
    package = {**locked, "id": f"{build.REGISTRY}#{name}@{version}",
               "manifest_path": str(source / "Cargo.toml")}
    return package, locked


class BuildGuards(unittest.TestCase):
    def test_p0_competing_compiler_and_source_overrides_remain_rejected(self):
        for variable in build.baseline.UNSAFE_OVERRIDES:
            with self.subTest(variable=variable), self.assertRaisesRegex(ValueError, "competing"):
                build.build_environment(Path("/private"), "2", {variable: "override"})
        with self.assertRaisesRegex(ValueError, "instrumented"):
            build.build_environment(Path("/private"), "2", {"NXRS_STD_CHANNEL_HOOK_STAGE": "1"})

    def test_private_paths_and_own_runtime_identity(self):
        out = Path("/private/event-path")
        env = build.build_environment(out, "2", {
            "RUSTFLAGS": "inherited flags", "NXRS_STD_CHANNEL_BUILD_ID": "old",
            "NXRS_EVENT_PATH_BUILD_ID": "old",
        })
        self.assertEqual(env["NXRS_STD_CHANNEL_HOOK_STAGE"], "0")
        for key in ("NXRS_SHARED_NUTTX_STD_SYSROOT", "NXRS_SHARED_CARGO_TARGET_DIR", "CARGO_TARGET_DIR"):
            self.assertTrue(Path(env[key]).is_relative_to(out))
        for key in ("RUSTFLAGS", "NXRS_STD_CHANNEL_BUILD_ID", "NXRS_EVENT_PATH_BUILD_ID"):
            self.assertNotIn(key, env)

    def test_firmware_command_selects_actual_fixture_and_reuses_native_adapter(self):
        command = build.firmware_command("pico2-mock", Path("/private"), "o2", "native-counter")
        for option, expected in (("--app-manifest", build.MANIFEST),
                                 ("--app-package", "nxrs-event-path-perf"),
                                 ("--bin", "event-path-perf"),
                                 ("--command", "event_path_perf"),
                                 ("--target-c-source", build.BASE / "native.c")):
            self.assertEqual(command[command.index(option) + 1], expected)
        self.assertIn("ARCH_PERF_EVENTS", command)
        self.assertNotIn("std_channel_perf", command)
        self.assertNotIn("NXRS_CARGO_CONFIG", command)

    def test_fixture_is_accepted_by_existing_deployment_resolution(self):
        spec = importlib.util.spec_from_file_location(
            "event_path_deployment_tests", build.ROOT / "tools/check-deployment.py")
        deployment = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(deployment)
        self.assertEqual(deployment.workspace_root(build.MANIFEST), build.ROOT)
        for platform in build.PLATFORMS:
            with self.subTest(platform=platform):
                result = deployment.validate(build.MANIFEST, platform, "nuttx")
                self.assertEqual(result["app_package"], build.PACKAGE)

    def test_real_lock_and_source_inventory_cover_actual_dependencies(self):
        self.assertEqual(build.locked_packages()["crossbeam-channel"]["version"], "0.5.17")
        inventory = build.inventory(build.resolve_platform("mps2-an521-mock", build.ROOT))
        for path in ("Cargo.lock", "shared/messaging/src/channel.rs", "hal/imu/src/api.rs",
                     "platform/hal-build.rs", "tests/std-channel-perf/native.c",
                     "tests/std-channel-perf/rust/src/native.rs", "tests/event-path-perf/build.py",
                     "tests/event-path-perf/rust/Cargo.toml"):
            self.assertIn(path, inventory)
        self.assertNotIn("tests/std-channel-perf/rust/src/latency.rs", inventory)

    def test_single_core_and_native_clock_controls_remain_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config"
            config.write_text(CONFIG)
            self.assertEqual(build.validate_configuration(config, "o2")["CONFIG_SMP"], "n")
            config.write_text(CONFIG.replace("# CONFIG_SMP is not set", "CONFIG_SMP=y"))
            with self.assertRaisesRegex(ValueError, "single-core"):
                build.validate_configuration(config, "o2")
        with self.assertRaisesRegex(ValueError, "physical-board"):
            build.build(SimpleNamespace(out=Path("/unused"), platform="mps2-an521-mock",
                                        clock_source="native-counter"))


class CargoMetadataControls(unittest.TestCase):
    def cargo_fixture(self, root, stdout, stderr, returncode=0):
        prepared = root / "prepared"
        prepared.mkdir()
        cargo = root / "cargo"
        cargo.write_text(f"#!{sys.executable}\nimport sys\n"
                         f"sys.stdout.write({stdout!r})\nsys.stderr.write({stderr!r})\n"
                         f"sys.exit({returncode})\n")
        cargo.chmod(0o755)
        return prepared, cargo

    def test_registry_diagnostics_do_not_corrupt_metadata_stdout(self):
        metadata = {"packages": [], "resolve": {"nodes": []}}
        stdout = json.dumps(metadata) + "\n"
        stderr = "    Updating crates.io index\n Downloading crates ...\n"
        for sdk, cargo_sdk in (("nightly-2025-09-15", "nightly-2025-09-15"),
                               ("esp-1.90.0.0", "1.90.0")):
            with self.subTest(sdk=sdk), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                prepared, cargo = self.cargo_fixture(root, stdout, stderr)
                with patch.object(build, "run", return_value=str(cargo)) as lookup:
                    self.assertEqual(build.cargo_metadata(prepared, sdk, {}), metadata)
                lookup.assert_called_once_with(
                    ["rustup", "which", "--toolchain", cargo_sdk, "cargo"], env={})
                log = (root / "cargo-metadata.log").read_text()
                self.assertIn(stdout, log)
                self.assertIn(stderr, log)
                self.assertIn("exit status: 0", log)

    def test_nonzero_cargo_status_preserves_both_streams_and_refuses_json(self):
        stdout = '{"packages": []}\n'
        stderr = "error: failed to download crossbeam-channel\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared, cargo = self.cargo_fixture(root, stdout, stderr, 101)
            with patch.object(build, "run", return_value=str(cargo)), \
                    self.assertRaises(subprocess.CalledProcessError) as caught:
                build.cargo_metadata(prepared, "nightly-2025-09-15", {})
            self.assertEqual(caught.exception.returncode, 101)
            self.assertEqual(caught.exception.stdout, stdout)
            self.assertEqual(caught.exception.stderr, stderr)
            log = (root / "cargo-metadata.log").read_text()
            self.assertIn(stdout, log)
            self.assertIn(stderr, log)
            self.assertIn("exit status: 101", log)

    def test_malformed_stdout_is_not_recovered_from_stderr_or_json_suffix(self):
        for stdout, stderr in (("not JSON\n", '{"packages": []}\n'),
                               ('diagnostic\n{"packages": []}\n', "")):
            with self.subTest(stdout=stdout), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                prepared, cargo = self.cargo_fixture(root, stdout, stderr)
                with patch.object(build, "run", return_value=str(cargo)), \
                        self.assertRaises(json.JSONDecodeError):
                    build.cargo_metadata(prepared, "nightly-2025-09-15", {})
                log = (root / "cargo-metadata.log").read_text()
                self.assertIn(stdout, log)
                self.assertIn(stderr, log)


class RegistrySourceControls(unittest.TestCase):
    def test_receipt_checks_downloaded_archive_and_actual_source_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            package, locked = registry_fixture(Path(temporary))
            result = build.registry_source(package, locked)
            self.assertTrue(result["uninstrumented_archive_match"])
            self.assertEqual(result["registry_checksum"], locked["checksum"])
            self.assertEqual(result["source_sha256"]["src/lib.rs"], hashlib.sha256(
                (Path(result["source_root"]) / "src/lib.rs").read_bytes()).hexdigest())

    def test_hooked_missing_extra_or_substituted_sources_are_rejected(self):
        for mutation in ("hook", "missing", "extra", "archive", "source", "version", "symlink"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                package, locked = registry_fixture(root)
                source = Path(package["manifest_path"]).parent
                if mutation == "hook":
                    (source / "src/lib.rs").write_text("fn witness_hook() {}")
                elif mutation == "missing":
                    (source / "src/lib.rs").unlink()
                elif mutation == "extra":
                    (source / "build.rs").write_text("fn main() {}")
                elif mutation == "archive":
                    locked["checksum"] = "0" * 64
                elif mutation == "source":
                    package["source"] = None
                elif mutation == "version":
                    package["version"] = "0.5.16"
                elif mutation == "symlink":
                    content = (source / "src/lib.rs").read_bytes()
                    (source / "src/lib.rs").unlink()
                    (root / "outside.rs").write_bytes(content)
                    (source / "src/lib.rs").symlink_to(root / "outside.rs")
                with self.assertRaises(ValueError):
                    build.registry_source(package, locked)

    def metadata_fixture(self, root):
        channel, channel_lock = registry_fixture(root)
        utilities, utilities_lock = registry_fixture(root, "crossbeam-utils", "0.8.23")
        manifest = root / "Cargo.toml"
        manifest.write_text(f'[package]\nname="{build.PACKAGE}"\nversion="0.1.0"\n'
                            '[dependencies]\ncrossbeam-channel="=0.5.17"\n')
        lock = root / "Cargo.lock"
        lock.write_text("\n".join(
            '[[package]]\n' + '\n'.join(f'{key}={json.dumps(value)}' for key, value in item.items())
            for item in (channel_lock, utilities_lock)))
        application = {"id": "fixture-app", "name": build.PACKAGE, "manifest_path": str(manifest)}
        metadata = {"packages": [application, channel, utilities], "resolve": {"nodes": [
            {"id": application["id"], "deps": [{"name": "crossbeam_channel", "pkg": channel["id"]}]},
            {"id": channel["id"], "deps": [{"name": "crossbeam_utils", "pkg": utilities["id"]}]},
            {"id": utilities["id"], "deps": []},
        ]}}
        pins = {item["name"]: (item["version"], item["checksum"])
                for item in (channel_lock, utilities_lock)}
        return metadata, manifest, lock, pins

    def test_actual_selected_dependency_edges_and_lock_are_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            metadata, manifest, lock, pins = self.metadata_fixture(Path(temporary))
            with patch.object(build, "REGISTRY_PINS", pins):
                result = build.crossbeam_sources(metadata, manifest, lock)
                self.assertEqual([p["name"] for p in result["packages"]],
                                 ["crossbeam-channel", "crossbeam-utils"])
                wrong = copy.deepcopy(metadata)
                wrong["resolve"]["nodes"][0]["deps"] = []
                with self.assertRaisesRegex(ValueError, "selected dependency"):
                    build.crossbeam_sources(wrong, manifest, lock)
                original = lock.read_text()
                lock.write_text(original.replace(pins["crossbeam-channel"][1], "0" * 64))
                with self.assertRaisesRegex(ValueError, "locked registry"):
                    build.crossbeam_sources(metadata, manifest, lock)
                lock.write_text(original)
                manifest.write_text(manifest.read_text().replace("=0.5.17", "0.5.17"))
                with self.assertRaisesRegex(ValueError, "exact registry"):
                    build.crossbeam_sources(metadata, manifest, lock)

    def test_compiler_artifact_must_name_each_selected_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package, locked = registry_fixture(root)
            source = build.registry_source(package, locked)
            artifact = {"reason": "compiler-artifact", "package_id": source["package_id"],
                        "target": {"kind": ["lib"],
                                   "src_path": str(Path(source["source_root"]) / "src/lib.rs")}}
            log = root / "cargo-messages.jsonl"
            log.write_text(json.dumps(artifact) + "\n")
            build.verify_compiled_sources(root, {"packages": [source]})
            for invalid in ([], [artifact, artifact], [{**artifact, "target": {
                    "kind": ["lib"], "src_path": "/different/src/lib.rs"}}]):
                log.write_text("\n".join(json.dumps(item) for item in invalid))
                with self.assertRaisesRegex(ValueError, "did not compile"):
                    build.verify_compiled_sources(root, {"packages": [source]})


class BuildReceiptControls(unittest.TestCase):
    def run_fixture(self, root, failure=None):
        source = root / "sdk"
        source.mkdir()
        out = root / "out"
        args = SimpleNamespace(out=out, platform="mps2-an521-mock", app_opt_level="2",
                               kernel_optimization="o2", source_sysroot=source, clock_source="baseline")
        profile = {"target": "thumbv8m.main-nuttx-eabi", "image": "nuttx", "sources": []}
        hook = copy.deepcopy(HOOK)
        if failure == "instrumented":
            hook["channel_instrumented"] = True

        def prepare(original, prepared, sdk, mode):
            self.assertEqual(mode, "latency")
            prepared.mkdir()
            (prepared / "std-channel-hook.json").write_text(json.dumps(hook))
            (prepared / "std-library-sources.json").write_text("{}")
            return hook

        def run(argv, **kwargs):
            if argv[:2] == ["git", "rev-parse"]:
                return "1" * 40
            if argv[:2] == ["git", "status"]:
                return " M tests/event-path-perf/build.py"
            if failure == "compile":
                raise ValueError("synthetic compiler failure")
            firmware = out / "firmware"
            (firmware / "nuttx").mkdir(parents=True)
            (firmware / "nuttx/nuttx").write_bytes(b"synthetic ELF; no firmware execution")
            (firmware / "resolved.config").write_text(CONFIG)
            for name in ("provenance.txt", "std-source.json", "std-patch.json", "abi-report.json",
                         "cargo-messages.jsonl"):
                (firmware / name).write_text("synthetic regression fixture")
            self.assertEqual(kwargs["env"]["NXRS_STD_CHANNEL_HOOK_STAGE"], "0")
            self.assertNotIn("NXRS_STD_CHANNEL_BUILD_ID", kwargs["env"])
            self.assertEqual(len(kwargs["env"]["NXRS_EVENT_PATH_BUILD_ID"]), 64)
            return ""

        sources = {"schema": "event-path-registry-sources-v1", "packages": []}
        inputs = [{"fixture": "a" * 64}, {"fixture": ("b" if failure == "inputs" else "a") * 64}]
        registries = [sources, {**sources, **({"changed": True} if failure == "registry" else {})}]
        with patch.object(build, "resolve_platform", return_value=profile), \
                patch.object(build, "inventory", side_effect=inputs), \
                patch.object(build, "run", side_effect=run), \
                patch.object(build.private_std, "prepare", side_effect=prepare), \
                patch.object(build, "cargo_metadata", return_value={}), \
                patch.object(build, "crossbeam_sources", side_effect=registries), \
                patch.object(build, "verify_selected_std", return_value=hook), \
                patch.object(build, "verify_compiled_sources"), \
                patch.dict(build.os.environ, {}, clear=True):
            if failure:
                with self.assertRaises(ValueError):
                    build.build(args)
            else:
                build.build(args)
        return out, json.loads((out / "build.json").read_text())

    def test_build_hashes_artifacts_without_claiming_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            out, record = self.run_fixture(Path(temporary))
            self.assertEqual(record["schema"], "event-path-build-v1")
            self.assertEqual(record["status"], "complete")
            self.assertEqual(record["runtime_kind"], "unmeasured")
            self.assertEqual(record["command"], "event_path_perf")
            self.assertEqual(record["image_sha256"], build.digest(out / record["image_file"]))
            self.assertEqual(record["std_array_sha256"], record["std_array_original_sha256"])
            self.assertIn("crossbeam-source.json", record["artifacts_sha256"])

    def test_compile_instrumentation_and_source_drift_retain_failed_receipts(self):
        for failure in ("compile", "inputs", "registry", "instrumented"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                _, record = self.run_fixture(Path(temporary), failure)
                self.assertEqual(record["status"], "failed")
                self.assertTrue(record["failure"])
                self.assertEqual(record["runtime_kind"], "unmeasured")


if __name__ == "__main__":
    unittest.main()
