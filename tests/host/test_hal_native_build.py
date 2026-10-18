"""Unit tests for optional native HAL compilation without Apple tooling."""

import subprocess
import sys
import tempfile
import unittest
import platform
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))
import hal_native  # noqa: E402


class NativeHalBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.capability = self.root / "wifi"
        self.ffi = self.capability / "ffi"
        self.sources = self.ffi / "macos"
        self.sources.mkdir(parents=True)
        self.out = self.root / "out"
        (self.capability / "Cargo.toml").write_text(
            '[package.metadata.nxrs.native.macos]\nframeworks = ["Foundation", "CoreWLAN"]\n'
        )

    def test_empty_source_set_skips_compiler_but_tracks_future_first_source(self):
        with mock.patch.object(hal_native.subprocess, "run") as run:
            directives = hal_native.build(self.capability, "macos", "x86_64-apple-darwin", self.out)
            self.assertIn(f'cargo:rerun-if-changed={self.sources}', directives)
            self.assertFalse(any('rustc-link' in line for line in directives))
        run.assert_not_called()

    def test_missing_source_directory_tracks_its_existing_parent(self):
        self.sources.rmdir()
        with mock.patch.object(hal_native.subprocess, "run") as run:
            directives = hal_native.build(self.capability, "macos", "x86_64-apple-darwin", self.out)
        self.assertIn(f'cargo:rerun-if-changed={self.ffi}', directives)
        run.assert_not_called()

    @mock.patch.object(hal_native.sys, "platform", "darwin")
    def test_compiles_c_and_objc_archives_and_tracks_headers(self):
        (self.sources / "bridge.c").write_text('#include "bridge.h"\n')
        (self.sources / "bridge.m").write_text("@interface Bridge @end\n")
        (self.sources / "bridge.h").write_text("/* header */\n")
        (self.ffi / "native").mkdir()
        (self.ffi / "native" / "shared.h").write_text("/* shared */\n")

        def compile_or_archive(command, **kwargs):
            if command[0] == "ar":
                Path(command[2]).touch()

        with mock.patch.object(hal_native.subprocess, "run", side_effect=compile_or_archive) as run:
            directives = hal_native.build(
                self.capability, "macos", "arm64-apple-darwin", self.out
            )

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 3)
        self.assertEqual(commands[0][:8], [
            "xcrun", "--sdk", "macosx", "clang", "-target", "arm64-apple-darwin",
            "-Wall", "-Wextra",
        ])
        self.assertIn("-Werror", commands[0])
        self.assertNotIn("-fobjc-arc", commands[0])
        self.assertIn("-fobjc-arc", commands[1])
        self.assertNotEqual(commands[0][-1], commands[1][-1])
        self.assertEqual(commands[2][:2], ["ar", "crs"])
        self.assertIn("cargo:rustc-link-lib=static=nxrs_wifi_native", directives)
        self.assertIn("cargo:rustc-link-lib=framework=CoreWLAN", directives)
        self.assertIn("cargo:rustc-link-lib=framework=Foundation", directives)
        for name in ('DEVELOPER_DIR', 'SDKROOT', 'MACOSX_DEPLOYMENT_TARGET'):
            self.assertIn(f'cargo:rerun-if-env-changed={name}', directives)
        self.assertIn(f"cargo:rerun-if-changed={self.sources}", directives)
        self.assertIn(f"cargo:rerun-if-changed={self.ffi / 'native'}", directives)
        self.assertIn(f"cargo:rerun-if-changed={self.capability / 'Cargo.toml'}", directives)

    @mock.patch.object(hal_native.sys, "platform", "darwin")
    def test_success_without_a_new_archive_cannot_link_stale_members(self):
        (self.sources / "bridge.c").touch()
        self.out.mkdir()
        old = self.out / "libnxrs_wifi_native.a"
        old.write_bytes(b"old archive")
        with mock.patch.object(hal_native.subprocess, "run"):
            with self.assertRaisesRegex(RuntimeError, "produced no archive"):
                hal_native.build(self.capability, "macos", "x86_64-apple-darwin", self.out)
        self.assertEqual(old.read_bytes(), b"old archive")

    @mock.patch.object(hal_native.sys, "platform", "darwin")
    def test_compile_failure_has_no_link_directives(self):
        source = self.sources / "bridge.c"
        source.write_text("int bridge(void) { return 0; }\n")
        failure = subprocess.CalledProcessError(1, ["clang"], stderr="compile diagnostic")
        with mock.patch.object(hal_native.subprocess, "run", side_effect=failure) as run:
            with self.assertRaisesRegex(RuntimeError, "compilation failed.*bridge.c.*compile diagnostic"):
                hal_native.build(self.capability, "macos", "arm64-apple-darwin", self.out)
        self.assertEqual(run.call_count, 1)

    @mock.patch.object(hal_native.sys, "platform", "darwin")
    def test_archive_failure_is_reported(self):
        (self.sources / "bridge.c").write_text("int bridge(void) { return 0; }\n")
        failure = subprocess.CalledProcessError(2, ["ar"], stderr="archive diagnostic")
        with mock.patch.object(
            hal_native.subprocess, "run", side_effect=[None, failure]
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "archive creation failed.*archive diagnostic"):
                hal_native.build(self.capability, "macos", "arm64-apple-darwin", self.out)
        self.assertEqual(run.call_count, 2)

    def test_rejects_non_macos_target_with_sources(self):
        ios_sources = self.ffi / "ios"
        ios_sources.mkdir()
        (ios_sources / "bridge.c").touch()
        with mock.patch.object(hal_native.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "target OS 'ios'.*not supported"):
                hal_native.build(self.capability, "ios", "arm64-apple-ios", self.out)
        run.assert_not_called()

    def test_rejects_non_macos_host_with_sources(self):
        (self.sources / "bridge.c").touch()
        with mock.patch.object(hal_native.sys, "platform", "linux"), mock.patch.object(
            hal_native.platform, "mac_ver", return_value=("", (0, 0, 0), "")
        ), mock.patch.object(hal_native.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "only be compiled on a macOS host"):
                hal_native.build(self.capability, "macos", "arm64-apple-darwin", self.out)
        run.assert_not_called()

    def test_rejects_unsafe_framework_metadata(self):
        (self.sources / "bridge.c").touch()
        (self.capability / "Cargo.toml").write_text(
            '[package.metadata.nxrs.native.macos]\nframeworks = ["CoreWLAN -weak"]\n'
        )
        with mock.patch.object(hal_native.sys, "platform", "darwin"), mock.patch.object(
            hal_native.subprocess, "run"
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "frameworks must be an array"):
                hal_native.build(self.capability, "macos", "arm64-apple-darwin", self.out)
        run.assert_not_called()

    def _linux_fixture(self, metadata='pkg-config = ["gio-2.0"]'):
        linux = self.ffi / "linux"
        linux.mkdir(exist_ok=True)
        source = linux / "bridge.c"
        source.write_text('#include "sub/bridge.h"\nint bridge(void) { return 0; }\n')
        (linux / "sub").mkdir(exist_ok=True)
        (linux / "sub" / "bridge.h").write_text("/* nested header */\n")
        (self.ffi / "native").mkdir(exist_ok=True)
        (self.ffi / "native" / "shared.h").write_text("/* shared header */\n")
        (self.capability / "Cargo.toml").write_text(
            f"[package.metadata.nxrs.native.linux]\n{metadata}\n"
        )
        arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(platform.machine().lower(), platform.machine().lower())
        return linux, source, f"{arch}-unknown-linux-gnu"

    @mock.patch.object(hal_native.sys, "platform", "linux")
    def test_linux_gio_compile_link_and_recursive_inputs(self):
        linux, source, target = self._linux_fixture()
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if command[0] == "pkg-config":
                output = "-I/usr/include/glib-2.0 -D_GNU_SOURCE" if "--cflags" in command else "-L/usr/lib -lgio-2.0 -lgobject-2.0 -pthread"
                return mock.Mock(stdout=output, stderr="")
            if "-c" in command:
                Path(command[command.index("-o") + 1]).touch()
            elif "crs" in command:
                Path(command[command.index("crs") + 1]).touch()
            return mock.Mock(stdout="", stderr="")

        with mock.patch.object(hal_native.subprocess, "run", side_effect=run):
            directives = hal_native.build(self.capability, "linux", target, self.out)

        self.assertEqual(calls[0], ["pkg-config", "--cflags", "gio-2.0"])
        self.assertEqual(calls[1], ["pkg-config", "--libs", "gio-2.0"])
        compile_command = calls[2]
        self.assertEqual(compile_command[0], "cc")
        self.assertIn("-I/usr/include/glib-2.0", compile_command)
        self.assertIn("-D_GNU_SOURCE", compile_command)
        self.assertEqual(calls[-1][0:2], ["ar", "crs"])
        self.assertIn("cargo:rustc-link-lib=gio-2.0", directives)
        self.assertIn("cargo:rustc-link-lib=gobject-2.0", directives)
        self.assertIn("cargo:rustc-link-search=native=/usr/lib", directives)
        self.assertIn("cargo:rustc-link-lib=pthread", directives)
        self.assertIn(f"cargo:rerun-if-changed={linux / 'sub' / 'bridge.h'}", directives)
        self.assertIn(f"cargo:rerun-if-changed={self.ffi / 'native' / 'shared.h'}", directives)
        for name in ("PKG_CONFIG", "PKG_CONFIG_PATH", "CC", "AR"):
            self.assertIn(f"cargo:rerun-if-env-changed={name}", directives)
        self.assertIn(f"cargo:rerun-if-changed={source}", directives)

    @mock.patch.object(hal_native.sys, "platform", "linux")
    def test_linux_pkg_config_failure_stops_before_compilation(self):
        _, _, target = self._linux_fixture()
        failure = subprocess.CalledProcessError(1, ["pkg-config"], stderr="gio missing")
        with mock.patch.object(hal_native.subprocess, "run", side_effect=failure) as run:
            with self.assertRaisesRegex(RuntimeError, "pkg-config cflags failed.*gio missing"):
                hal_native.build(self.capability, "linux", target, self.out)
        run.assert_called_once()

    @mock.patch.object(hal_native.sys, "platform", "linux")
    def test_linux_rejects_malformed_metadata_and_pkg_config_flags(self):
        _, _, target = self._linux_fixture('pkg-config = ["gio-2.0; touch nope"]')
        with mock.patch.object(hal_native.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "pkg-config must be an array"):
                hal_native.build(self.capability, "linux", target, self.out)
        run.assert_not_called()

        _, _, target = self._linux_fixture()
        with mock.patch.object(
            hal_native.subprocess, "run", return_value=mock.Mock(stdout="-Xunknown", stderr="")
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "unrecognized pkg-config cflags flag"):
                hal_native.build(self.capability, "linux", target, self.out)
        run.assert_called_once()

    def test_pkg_config_does_not_admit_options_or_unpropagated_link_arguments(self):
        self._linux_fixture('pkg-config = ["--static"]')
        with self.assertRaisesRegex(RuntimeError, "pkg-config must be an array"):
            hal_native._pkg_config(self.capability / "Cargo.toml", {"pkg-config": ["--static"]}, "linux")
        with self.assertRaisesRegex(RuntimeError, "unrecognized pkg-config libs flag"):
            hal_native._pkg_flags("-lgio-2.0 -Wl,--as-needed", "libs")
        with self.assertRaisesRegex(RuntimeError, "invalid CC command"):
            with mock.patch.dict(hal_native.os.environ, {"CC": "'unterminated"}):
                hal_native._env_command("CC", "cc")

    @mock.patch.object(hal_native.sys, "platform", "linux")
    def test_linux_rejects_system_library_abi_mismatch(self):
        _, _, target = self._linux_fixture()
        with mock.patch.object(hal_native.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "require a GNU target"):
                hal_native.build(self.capability, "linux", target.removesuffix("gnu") + "musl", self.out)
        run.assert_not_called()

    def test_linux_rejects_wrong_host_os_and_architecture(self):
        _, _, target = self._linux_fixture()
        with mock.patch.object(hal_native.sys, "platform", "darwin"), mock.patch.object(
            hal_native.subprocess, "run"
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "Linux sources can only be compiled on a Linux host"):
                hal_native.build(self.capability, "linux", target, self.out)
        run.assert_not_called()

        other_arch = "aarch64" if target.split("-", 1)[0] == "x86_64" else "x86_64"
        with mock.patch.object(hal_native.sys, "platform", "linux"), mock.patch.object(
            hal_native.subprocess, "run"
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "target architecture matching the host"):
                hal_native.build(self.capability, "linux", f"{other_arch}-unknown-linux-gnu", self.out)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
