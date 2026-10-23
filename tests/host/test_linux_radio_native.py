"""Linux native builds and private-bus tests; never uses the host radios."""
from pathlib import Path
import os
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from radio_rust import linux_wifi, linux_ble

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import hal_native  # noqa: E402


@unittest.skipUnless(sys.platform == "linux", "requires Linux GLib/GIO")
class LinuxRadioNativeTests(unittest.TestCase):
    def setUp(self):
        self.compiler = shlex.split(os.environ.get("CC", "cc"))
        self.pkg_config = shlex.split(os.environ.get("PKG_CONFIG", "pkg-config"))
        if (not self.compiler or not shutil.which(self.compiler[0]) or
                not self.pkg_config or not shutil.which(self.pkg_config[0])):
            self.skipTest("requires C compiler and pkg-config")
        if subprocess.run([*self.pkg_config, "--exists", "gio-2.0"], timeout=10).returncode:
            self.skipTest("requires GLib/GIO development headers (libglib2.0-dev)")

    def exercise(self, client=None, station=None, *, source="linux_wifi_native.c", timeout=15):
        self.assertIsNotNone(shutil.which("dbus-daemon"), "requires dbus-daemon")
        flags = subprocess.run([*self.pkg_config, "--cflags", "--libs", "gio-2.0"],
                               capture_output=True, text=True, check=True, timeout=10)
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "radio-native"
            library, native = (linux_wifi if source == "linux_wifi_native.c" else linux_ble)(Path(temporary))
            subprocess.run([
                *self.compiler, "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                "-fno-sanitize-recover=all", str(ROOT / "tests/host" / source),
                str(library), "-o", str(binary), *shlex.split(flags.stdout), *native,
            ], check=True, timeout=60)
            # GTestDBus owns a private daemon, including a crash watchdog. No
            # system service, saved profile, adapter, or permission is touched.
            result = subprocess.run([str(binary), *[str(item) for item in (client, station) if item]],
                                    capture_output=True, text=True, timeout=timeout)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result.stdout

    def test_wifi_private_bus_schemas_bounds_station_ownership_and_failures(self):
        self.exercise()

    def test_ble_private_bus_fresh_reports_filters_ownership_and_failures(self):
        self.exercise(source="linux_ble_native.c")

    def test_portable_rust_owner_positive_and_injected_failure(self):
        self.assertIsNotNone(shutil.which("cargo"), "requires the pinned Rust toolchain")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            env = os.environ.copy()
            # No inherited cross-target flags or product selection may relabel
            # an unrelated executable as the Linux provider under test.
            for key in ("RUSTC", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "CARGO_BUILD_TARGET"):
                env.pop(key, None)
            env.update(NXRS_PLATFORM="linux", CARGO_TARGET_DIR=str(directory), CARGO_BUILD_JOBS="1")

            def build(command, target):
                built = subprocess.run([
                    "cargo", "+1.90.0", *command, "--locked", "--offline",
                    "--message-format=json-render-diagnostics",
                ], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
                self.assertEqual(built.returncode, 0, built.stderr)
                records = [json.loads(line) for line in built.stdout.splitlines() if line]
                artifacts = [item for item in records if item.get("reason") == "compiler-artifact"
                             and item["target"]["name"] == target and item.get("executable")]
                self.assertEqual(len(artifacts), 1)
                self.assertFalse(artifacts[0]["fresh"])
                binary = Path(artifacts[0]["executable"]).resolve()
                self.assertTrue(binary.is_relative_to(directory))
                return binary

            scan = build(["build", "-p", "nxrs-std-wifi", "--bin", "nxrs-std-wifi"], "nxrs-std-wifi")
            station = build(["test", "--no-run", "-p", "nxrs-wifi", "--test", "linux_native"], "linux_native")
            self.exercise(scan, station)
            ble = build(["build", "-p", "nxrs-std-ble", "--bin", "nxrs-std-ble"], "nxrs-std-ble")
            gatt = build(["build", "-p", "nxrs-std-ble", "--bin", "gatt-client"], "gatt-client")
            output = self.exercise(ble, gatt, source="linux_ble_native.c", timeout=60)
            # Reuse the public qualification oracle, including ordered phases,
            # exact workload/value records and every teardown confirmation.
            import importlib.util
            spec = importlib.util.spec_from_file_location("gatt_oracle", ROOT / "tests/nuttx-std/ble/client.py")
            oracle = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(oracle)
            cases = []
            for section in output.split("END\n"):
                if not section:
                    continue
                header, *lines = section.splitlines()
                self.assertTrue(header.startswith("CASE "))
                case = header.removeprefix("CASE ")
                records = oracle._ClientRecords(case)
                for line in lines:
                    records.observe(line.encode())
                records.finish()
                cases.append(case)
            self.assertEqual(cases, ["legacy", "cancel", "values", "full"])

    def test_real_native_archive_and_cargo_link_metadata(self):
        import platform
        machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(
            platform.machine().lower(), platform.machine().lower())
        for capability in ("wifi", "ble"):
            with self.subTest(capability=capability):
                self.link_native_archive(capability, machine)

    def link_native_archive(self, capability, machine):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directives = hal_native.build(ROOT / "hal" / capability, "linux",
                                          machine + "-unknown-linux-gnu", directory)
            flags = []
            for directive in directives:
                if directive.startswith("cargo:rustc-link-search=native="):
                    flags.append("-L" + directive.split("=", 2)[2])
                elif directive.startswith("cargo:rustc-link-lib="):
                    flags.append("-l" + directive.split("=", 1)[1].removeprefix("static="))
                elif directive.startswith("cargo:rustc-link-arg="):
                    flags.append(directive.split("=", 1)[1])
            source = directory / "link.c"
            source.write_text(f'extern int {capability}_gio_constant(unsigned);\nint main(void) {{ return {capability}_gio_constant(0) != 1; }}\n')
            binary = directory / "link"
            subprocess.run([*self.compiler, str(source),
                            "-o", str(binary), *flags], check=True, timeout=60)
            subprocess.run([str(binary)], check=True, timeout=5)


if __name__ == "__main__":
    unittest.main()
