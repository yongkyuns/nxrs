"""Build a handwritten, renamed consumer from only its declared Cargo packages."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).with_name("fixtures") / "external-consumer"


def dependencies(manifest):
    """Normal/build edges, including optional and target-specific declarations."""
    for table in [manifest, *manifest.get("target", {}).values()]:
        for kind in ("dependencies", "build-dependencies"):
            yield from table.get(kind, {}).values()


def without_dev_dependencies(text):
    # A dependency's own unit/integration tests are outside this consumer's
    # graph. Do not copy the NXRS test workspace merely to resolve their paths.
    result, omitted = [], False
    for line in text.splitlines(keepends=True):
        if line.startswith("["):
            omitted = "dev-dependencies" in line.split("]", 1)[0]
        if not omitted:
            result.append(line)
    return "".join(result)


def copy_dependency_closure(directory, pending):
    """Copy only declared normal/build packages, preserving relative path edges."""
    copied = set()
    while pending:
        source = pending.pop().resolve()
        if source in copied:
            continue
        copied.add(source)
        destination = directory / "deps" / source.relative_to(ROOT)
        shutil.copytree(source, destination, ignore=shutil.ignore_patterns("target", ".git"))
        text = without_dev_dependencies((source / "Cargo.toml").read_text())
        (destination / "Cargo.toml").write_text(text)
        pending.extend(source / dependency["path"] for dependency in dependencies(tomllib.loads(text))
                       if isinstance(dependency, dict) and "path" in dependency)
    return copied


class ExternalConsumerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="nxrs-external-")
        cls.directory = Path(cls.temporary.name).resolve()
        shutil.copytree(FIXTURE, cls.directory, dirs_exist_ok=True)
        pending = [ROOT / dependency["path"].removeprefix("deps/")
                   for dependency in dependencies(tomllib.loads((FIXTURE / "Cargo.toml").read_text()))
                   if isinstance(dependency, dict) and "path" in dependency]
        copy_dependency_closure(cls.directory, pending)
        cls.environment = os.environ.copy()
        for name in ("NXRS_PLATFORM", "NXRS_SDK_ROOT", "NXRS_RESOLVED_PROFILE",
                     "CARGO_BUILD_TARGET", "RUSTFLAGS",
                     "CARGO_ENCODED_RUSTFLAGS", "RUSTC", "NXRS_NUTTX_NATIVE_CONFIG"):
            cls.environment.pop(name, None)
        cls.environment.update(CARGO_TARGET_DIR=str(cls.directory / "target"))
        # Deliberately generate this application's lockfile; never copy Cargo.lock.
        cls.command("generate-lockfile", "--offline")
        cls.lock = (cls.directory / "Cargo.lock").read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @classmethod
    def command(cls, *arguments, environment=None, success=True):
        result = subprocess.run(["cargo", *arguments], cwd=cls.directory,
                                env=environment or cls.environment,
                                capture_output=True, text=True, timeout=180)
        if success and result.returncode:
            raise AssertionError(result.stderr)
        return result

    def test_detached_consumer_with_own_lock_and_renamed_dependencies(self):
        self.assertFalse((self.directory / "deps/platform/build").exists())
        self.assertFalse((self.directory / "deps/platform/profiles").exists())
        self.assertFalse((self.directory / "deps/driver/acquisition").exists())
        self.assertTrue((self.directory / "deps/hal/acquisition/Cargo.toml").is_file())
        self.assertFalse((self.directory / "deps/platform/hal-build.rs").exists())
        native = {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(sys.platform)
        for profile in dict.fromkeys((None, "mock", native)):
            with self.subTest(profile=profile):
                env = {**self.environment, "NXRS_SDK_ROOT": str(ROOT)}
                if profile:
                    env["NXRS_PLATFORM"] = profile
                records = self.build(env)
                compiled = {item["target"]["name"] for item in records
                            if item.get("reason") == "compiler-artifact"}
                self.assertIn("nxrs_build_support", compiled)
                self.assertIn("acquisition", compiled)
                self.assertNotIn("nxrs_build", compiled)
                self.assertNotIn("wifi", compiled)
                self.assertNotIn("ble", compiled)
                binary = self.executable(records)
                output = self.directory / ("portable-" + (profile or "unbound"))
                run = subprocess.run([str(binary), str(output), *(["--sensors"] if profile == "mock" else [])],
                                     env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertIn("external consumer:", run.stdout)
        self.assertEqual((self.directory / "Cargo.lock").read_bytes(), self.lock)

    def test_isolated_hal_packages_do_not_require_sibling_sensor_catalogs(self):
        for capability in ("imu", "gnss", "camera"):
            with self.subTest(capability=capability):
                directory = self.directory / ("isolated-" + capability)
                (directory / "src").mkdir(parents=True)
                (directory / "Cargo.toml").write_text(
                    '[package]\nname = "isolated-consumer"\nversion = "0.0.0"\nedition = "2021"\n'
                    '[workspace]\nresolver = "2"\n'
                    '[workspace.package]\nversion = "0.1.0"\nedition = "2021"\n'
                    '[dependencies]\n'
                    f'capability = {{ package = "nxrs-{capability}", path = "deps/hal/{capability}" }}\n')
                (directory / "src/main.rs").write_text(
                    'fn main() { assert!(std::mem::size_of::<capability::DeviceError>() > 0); }\n')
                copied = copy_dependency_closure(directory, [ROOT / "hal" / capability])
                for other in {"imu", "gnss", "camera"} - {capability}:
                    self.assertNotIn((ROOT / "hal" / other).resolve(), copied)
                    self.assertFalse((directory / "deps/hal" / other).exists())
                self.assertFalse((directory / "deps/platform/profiles").exists())
                self.assertFalse((directory / "deps/platform/build").exists())

                env = {**self.environment, "CARGO_TARGET_DIR": str(self.directory / "isolated-target")}
                lock = subprocess.run(["cargo", "generate-lockfile", "--offline"], cwd=directory,
                                      env=env, capture_output=True, text=True, timeout=180)
                self.assertEqual(lock.returncode, 0, lock.stderr)
                for profile in (None, "mock"):
                    with self.subTest(capability=capability, profile=profile):
                        selected = dict(env)
                        if profile:
                            # The explicit SDK provides the complete profile. Its
                            # other HAL bindings must not add compiled packages.
                            selected.update(NXRS_PLATFORM=profile, NXRS_SDK_ROOT=str(ROOT))
                        result = subprocess.run(
                            ["cargo", "build", "--offline", "--locked", "--message-format=json"],
                            cwd=directory, env=selected, capture_output=True, text=True, timeout=180)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        records = [json.loads(line) for line in result.stdout.splitlines() if line]
                        compiled = {item["target"]["name"] for item in records
                                    if item.get("reason") == "compiler-artifact"}
                        self.assertIn(capability, compiled)
                        for other in {"imu", "gnss", "camera"} - {capability}:
                            self.assertNotIn(other, compiled)
                        self.assertNotIn("nxrs_build", compiled)

    def test_product_frontend_builds_the_detached_mock_consumer(self):
        compiler_output = self.directory / "frontend"
        result = subprocess.run(
            ["cargo", "build", "--offline", "--locked", "-p", "nxrs-build",
             "--target-dir", str(compiler_output)],
            cwd=ROOT, env=self.environment, capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        suffix = ".exe" if sys.platform == "win32" else ""
        frontend = compiler_output / "debug" / ("nxrs-build" + suffix)
        output = self.directory / "product"
        result = subprocess.run(
            [str(frontend), "--workspace-root", str(ROOT), "build", "--profile", "mock",
             "--manifest-path", str(self.directory / "Cargo.toml"), "--out", str(output)],
            cwd=self.directory, env=self.environment, capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = output / "resolved-profile.json"
        binary = output / "cargo/release" / ("independent-product" + suffix)
        run = subprocess.run([str(binary), str(self.directory / "receipt-output"), "--sensors"],
                             env=self.environment, capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

        # Cargo must fail before acquisition if given the right profile for the wrong target.
        data = json.loads(receipt.read_text())
        data["target"] = "wrong-target"
        receipt.write_text(json.dumps(data))
        env = {**self.environment, "NXRS_SDK_ROOT": str(ROOT), "NXRS_PLATFORM": "mock",
               "NXRS_RESOLVED_PROFILE": str(receipt)}
        result = self.command("build", "--offline", "--locked", environment=env, success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("resolved profile receipt target does not match", result.stderr)
        self.assertEqual((self.directory / "Cargo.lock").read_bytes(), self.lock)

    def test_selected_profile_requires_explicit_sdk_root_outside_checkout(self):
        env = {**self.environment, "NXRS_PLATFORM": "mock"}
        result = self.command("build", "--offline", "--locked", environment=env, success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Set NXRS_SDK_ROOT", result.stderr)

    @unittest.skipUnless(sys.platform == "linux", "Linux GIO ownership")
    def test_both_linux_radio_packages_share_one_native_owner(self):
        env = {**self.environment, "NXRS_PLATFORM": "linux", "NXRS_SDK_ROOT": str(ROOT)}
        records = self.build(env, "--features", "radios")
        compiled = [item["target"]["name"] for item in records
                    if item.get("reason") == "compiler-artifact" and "lib" in item["target"]["kind"]]
        self.assertEqual(compiled.count("linux_gio"), 1)
        self.assertIn("wifi", compiled)
        self.assertIn("ble", compiled)
        self.assertNotIn("nxrs_build", compiled)
        owners = [item for item in records if item.get("reason") == "build-script-executed"
                  and any(library.startswith("static=") and library.endswith("_native")
                          for library in item["linked_libs"])]
        self.assertEqual(len(owners), 1)
        self.assertIn("nxrs-linux-gio", owners[0]["package_id"])
        binary = self.executable(records)
        run = subprocess.run([str(binary), str(self.directory / "radios-linux")], env=env,
                             capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    @unittest.skipUnless(sys.platform == "linux", "Linux GIO client selection")
    def test_unused_radio_binding_does_not_require_the_native_sdk(self):
        sdk = self.directory / "profile-sdk"
        profiles = sdk / "platform/profiles"
        profiles.mkdir(parents=True)
        (sdk / "platform/nuttx/platforms").mkdir(parents=True)
        (sdk / "hal").symlink_to(self.directory / "deps/hal", target_is_directory=True)
        (sdk / "Cargo.toml").write_text("[workspace]\n")
        for used, unused in [("wifi", "ble"), ("ble", "wifi")]:
            with self.subTest(client=used):
                (profiles / "unbound-client.toml").write_text(
                    f'target-os = ["linux"]\n[hal]\n{used} = "none"\n{unused} = "linux"\n')
                (profiles / "bound-client.toml").write_text(
                    f'target-os = ["linux"]\n[hal]\n{used} = "linux"\n{unused} = "none"\n')
                env = {**self.environment, "NXRS_PLATFORM": "unbound-client",
                       "NXRS_SDK_ROOT": str(sdk),
                       "PKG_CONFIG": "nxrs-no-pkg-config-in-contract-only-build"}
                records = self.build(env, "--features", used)
                owners = [row for row in records if row.get("reason") == "build-script-executed"
                          and "nxrs-linux-gio" in row["package_id"]]
                self.assertEqual(len(owners), 1)
                self.assertEqual(owners[0]["linked_libs"], [])
                # The same consumed client bound to Linux must require its SDK;
                # this guards against accidentally disabling native builds.
                env["NXRS_PLATFORM"] = "bound-client"
                result = self.command("build", "--offline", "--locked", "--features", used,
                                      environment=env, success=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("nxrs-linux-gio", result.stderr)

    def build(self, environment, *arguments):
        result = self.command("build", "--offline", "--locked", "--message-format=json", *arguments,
                              environment=environment)
        return [json.loads(line) for line in result.stdout.splitlines() if line]

    def executable(self, records):
        return Path(next(item["executable"] for item in records
                         if item.get("reason") == "compiler-artifact"
                         and item["target"]["name"] == "independent-product"
                         and item.get("executable")))


if __name__ == "__main__":
    unittest.main()
