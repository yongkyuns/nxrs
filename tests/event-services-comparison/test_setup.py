"""Offline tests for pinned event-services toolchain setup."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("event_services_setup", HERE / "setup.py")
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class HostAndPlatformTests(unittest.TestCase):
    def test_build_command_forwards_lean_mode_without_changing_workload(self):
        for platform in ("zephyr", "embassy"):
            full = setup.build_command(platform, Path("cache"), Path("image"))
            lean = setup.build_command(platform, Path("cache"), Path("image"), instrumentation="lean")
            index = full.index("--instrumentation") + 1
            self.assertEqual(full[index], "full")
            self.assertEqual(lean[index], "lean")
            self.assertEqual(full[:index] + full[index+1:], lean[:index] + lean[index+1:])
    def test_host_key_normalizes_supported_systems_and_architectures(self):
        cases = (
            ("Darwin", "x86_64", "macos-x86_64"),
            ("Darwin", "arm64", "macos-aarch64"),
            ("Linux", "x86_64", "linux-x86_64"),
            ("Linux", "aarch64", "linux-aarch64"),
        )
        for system, machine, expected in cases:
            with self.subTest(system=system, machine=machine):
                self.assertEqual(setup.host_key(system, machine), expected)

    def test_host_key_rejects_unsupported_system_and_architecture(self):
        for system, machine in (("Windows", "x86_64"),
                                ("Darwin", "i386"),
                                ("FreeBSD", "aarch64")):
            with self.subTest(system=system, machine=machine), self.assertRaises(ValueError):
                setup.host_key(system, machine)

    def test_selected_platforms_expands_all_in_stable_order(self):
        self.assertEqual(setup.selected_platforms("zephyr"), ("zephyr",))
        self.assertEqual(setup.selected_platforms("embassy"), ("embassy",))
        self.assertEqual(setup.selected_platforms("all"), ("zephyr", "embassy"))


class AssetSelectionTests(unittest.TestCase):
    def test_zephyr_and_embassy_assets_are_separate_and_all_is_the_union(self):
        for host in setup.PINS["hosts"]:
            with self.subTest(host=host):
                zephyr = setup.assets(host, ("zephyr",))
                embassy = setup.assets(host, ("embassy",))
                combined = setup.assets(host, ("zephyr", "embassy"))
                self.assertEqual(set(zephyr), {"sdk", "zephyr-toolchain"})
                self.assertEqual(set(embassy), {"gcc", "rust", "rust-src", "espflash"})
                self.assertEqual(set(combined), set(zephyr) | set(embassy))
                for pin in combined.values():
                    self.assertTrue(pin["url"].startswith("https://"))
                    self.assertRegex(pin["sha256"], r"^[0-9a-f]{64}$")


class DownloadTests(unittest.TestCase):
    def test_non_https_url_is_rejected_before_opening_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "archive.tar.gz"
            with patch.object(setup.urllib.request, "urlopen") as urlopen:
                with self.assertRaises(ValueError):
                    setup.download("http://example.invalid/archive", "0" * 64, destination)
            urlopen.assert_not_called()
            self.assertFalse(destination.exists())

    def test_valid_cache_is_reused_and_corrupt_cache_raises(self):
        payload = b"cached archive"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "archive"
            destination.write_bytes(payload)
            with patch.object(setup.urllib.request, "urlopen") as urlopen:
                setup.download("https://example.invalid/archive", digest, destination)
            urlopen.assert_not_called()

            destination.write_bytes(b"corrupt")
            with patch.object(setup.urllib.request, "urlopen") as urlopen:
                with self.assertRaises(ValueError):
                    setup.download("https://example.invalid/archive", digest, destination)
            urlopen.assert_not_called()
            self.assertEqual(destination.read_bytes(), b"corrupt")

    def test_new_download_is_hash_verified_before_atomic_publish(self):
        payload = b"verified archive payload"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "cache" / "archive.tar.gz"
            response = io.BytesIO(payload)
            with patch.object(setup.urllib.request, "urlopen", return_value=response) as urlopen:
                setup.download("https://example.invalid/archive", digest, destination)
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(list(destination.parent.iterdir()), [destination])
            urlopen.assert_called_once()

    def test_bad_download_digest_does_not_publish_partial_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "archive"
            response = io.BytesIO(b"untrusted bytes")
            with patch.object(setup.urllib.request, "urlopen", return_value=response):
                with self.assertRaises(ValueError):
                    setup.download("https://example.invalid/archive", "0" * 64, destination)
            self.assertFalse(destination.exists())
            self.assertEqual(list(Path(temporary).iterdir()), [])


def make_tar(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, kind, target in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(target)
                archive.addfile(info, io.BytesIO(target))
            else:
                info.type = tarfile.SYMTYPE
                info.linkname = target
                archive.addfile(info)
    return stream.getvalue()


class ExtractTests(unittest.TestCase):
    def test_extracts_safe_zip_and_tar_archives(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            zip_path = root / "source.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("package/bin/tool", b"tool")
            zip_out = root / "zip-out"
            setup.extract(zip_path, zip_out)
            self.assertEqual((zip_out / "package/bin/tool").read_bytes(), b"tool")

            tar_path = root / "source.tar.gz"
            tar_path.write_bytes(make_tar([("package/readme", "file", b"safe")]))
            tar_out = root / "tar-out"
            setup.extract(tar_path, tar_out)
            self.assertEqual((tar_out / "package/readme").read_bytes(), b"safe")

    def test_tar_component_omission_keeps_selected_rust_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "rust.tar.gz"
            archive.write_bytes(make_tar([
                ("rust-dist/rust-docs/file", "file", b"large docs"),
                ("rust-dist/rustc/file", "file", b"compiler"),
            ]))
            destination = root / "rust"
            setup.extract(archive, destination, omit=("rust-docs",))
            self.assertFalse((destination / "rust-dist/rust-docs").exists())
            self.assertEqual((destination / "rust-dist/rustc/file").read_bytes(), b"compiler")

    def test_existing_destination_is_refused_without_modification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "source.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("file", b"new")
            destination = root / "destination"
            destination.mkdir()
            sentinel = destination / "keep"
            sentinel.write_bytes(b"existing")
            with self.assertRaises(ValueError):
                setup.extract(archive_path, destination)
            self.assertEqual(sentinel.read_bytes(), b"existing")

    def test_zip_and_tar_path_traversal_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            zip_path = root / "traversal.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("../escaped", b"bad")
            tar_path = root / "traversal.tar.gz"
            tar_path.write_bytes(make_tar([("../escaped", "file", b"bad")]))
            for archive_path in (zip_path, tar_path):
                destination = root / (archive_path.stem + "-out")
                with self.subTest(archive=archive_path.name), self.assertRaises(ValueError):
                    setup.extract(archive_path, destination)
                self.assertFalse((root / "escaped").exists())

    def test_unsafe_tar_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "symlink.tar.gz"
            archive_path.write_bytes(make_tar([("package/link", "symlink", "../../outside")]))
            destination = root / "out"
            with self.assertRaises((ValueError, tarfile.FilterError)):
                setup.extract(archive_path, destination)
            self.assertFalse(destination.exists())


class CheckoutTests(unittest.TestCase):
    def git(self, *args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def make_repository(self, root, name, content):
        repository = root / name
        repository.mkdir()
        self.git("init", "-q", cwd=repository)
        self.git("config", "user.name", "Offline Test", cwd=repository)
        self.git("config", "user.email", "offline@example.invalid", cwd=repository)
        (repository / "source.txt").write_text(content)
        self.git("add", "source.txt", cwd=repository)
        self.git("commit", "-qm", "fixture", cwd=repository)
        return repository, self.git("rev-parse", "HEAD", cwd=repository).stdout.strip()

    def test_checkout_uses_local_source_and_verifies_requested_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, revision = self.make_repository(root, "source", "pinned\n")
            destination = root / "checkout"
            setup.checkout(destination, str(source), revision)
            self.assertEqual((destination / "source.txt").read_text(), "pinned\n")
            head = self.git("rev-parse", "HEAD", cwd=destination).stdout.strip()
            self.assertEqual(head, revision)

    def test_checkout_refuses_dirty_or_wrong_revision_existing_worktree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, revision = self.make_repository(root, "source", "pinned\n")
            other_source, other_revision = self.make_repository(root, "other", "other\n")
            destination = root / "checkout"
            setup.checkout(destination, str(source), revision)

            with self.subTest(reason="dirty"):
                (destination / "source.txt").write_text("local change\n")
                with self.assertRaises(ValueError):
                    setup.checkout(destination, str(source), revision)
                (destination / "source.txt").write_text("pinned\n")

            with self.subTest(reason="wrong revision"):
                with self.assertRaises(ValueError):
                    setup.checkout(destination, str(other_source), other_revision)
                self.assertEqual(self.git("rev-parse", "HEAD", cwd=destination).stdout.strip(),
                                 revision)

    def test_fetch_failure_leaves_no_partial_checkout_or_staging_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, revision = self.make_repository(root, "source", "pinned\n")
            parent = root / "checkouts"
            destination = parent / "zephyr"
            real_run = setup.run

            def fail_fetch(argv, **kwargs):
                if list(map(str, argv[:2])) == ["git", "fetch"]:
                    raise subprocess.CalledProcessError(128, list(map(str, argv)))
                return real_run(argv, **kwargs)

            with patch.object(setup, "run", side_effect=fail_fetch):
                with self.assertRaises(subprocess.CalledProcessError):
                    setup.checkout(destination, str(source), revision)
            self.assertFalse(destination.exists())
            self.assertEqual(list(parent.iterdir()), [])


class PackageSafetyTests(unittest.TestCase):
    def test_package_rejects_symlink_destination_or_hash_marker(self):
        for kind in ("destination", "marker"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                packages = root / "packages"
                packages.mkdir()
                destination = packages / "tool"
                outside = root / "outside"
                if kind == "destination":
                    outside.mkdir()
                    destination.symlink_to(outside, target_is_directory=True)
                else:
                    destination.mkdir()
                    outside.write_text("preserve")
                    (destination / ".nxrs-archive-sha256").symlink_to(outside)
                with patch.object(setup, "download", return_value=root / "archive"), \
                     patch.object(setup, "extract") as extract:
                    with self.assertRaises(ValueError):
                        setup.package(root, "tool", {"url": "https://example.invalid/tool",
                                                      "sha256": "0" * 64,
                                                      "filename": "tool.tar.xz"})
                extract.assert_not_called()
                if kind == "marker":
                    self.assertEqual(outside.read_text(), "preserve")


class BuildCommandTests(unittest.TestCase):
    def test_build_command_targets_existing_build_script_and_supported_platform(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            out = root / "out"
            for platform in ("zephyr", "embassy"):
                with self.subTest(platform=platform):
                    command = setup.build_command(platform, root, out, layout="three")
                    self.assertIsInstance(command, list)
                    self.assertTrue(all(isinstance(arg, str) for arg in command))
                    self.assertIn("build.py", " ".join(command))
                    self.assertIn(str(HERE / "build.py"), command)
                    self.assertIn("--platform", command)
                    self.assertEqual(command[command.index("--platform") + 1],
                                     "zephyr-c" if platform == "zephyr" else "embassy")
                    self.assertIn("--layout", command)
                    self.assertEqual(command[command.index("--layout") + 1], "three")
                    self.assertIn("--out", command)
                    self.assertEqual(Path(command[command.index("--out") + 1]), out)
                    if platform == "zephyr":
                        self.assertIn("--zephyr", command)
                        self.assertIn(str(root / "sources/zephyr"), command)
                        self.assertNotIn("--cargo-target", command)
                    else:
                        self.assertIn("--cargo-target", command)
                        self.assertEqual(Path(command[command.index("--cargo-target") + 1]),
                                         root / "cargo-target")
                        self.assertNotIn("--zephyr", command)

    def test_build_command_rejects_unknown_platform(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                setup.build_command("nuttx", Path(temporary), Path(temporary) / "out")


class RootAndEnvironmentTests(unittest.TestCase):
    def test_prepare_root_rejects_nonignored_repo_path_and_unmanaged_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            with patch.object(setup, "ROOT", repository.resolve()):
                nonignored = repository / "build" / "dependencies"
                with self.assertRaises(ValueError):
                    setup.prepare_root(nonignored, "macos-aarch64")
                self.assertFalse(nonignored.exists())

                unmanaged = repository / "target" / "rtos-comparison"
                unmanaged.mkdir(parents=True)
                sentinel = unmanaged / "keep.txt"
                sentinel.write_text("user data")
                with self.assertRaises(ValueError):
                    setup.prepare_root(unmanaged, "macos-aarch64")
                self.assertEqual(sentinel.read_text(), "user data")
                self.assertFalse((unmanaged / ".nxrs-comparison.json").exists())

    def test_prepare_root_refuses_a_symlink_management_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            root = repository / "target" / "rtos-comparison"
            root.mkdir(parents=True)
            marker = root / ".nxrs-comparison.json"
            outside = Path(temporary) / "outside-marker.json"
            identity = {"schema": 1, "host": "macos-aarch64",
                        "pins_sha256": setup.digest(setup.PIN_FILE)}
            original = json.dumps(identity, separators=(",", ":"))
            outside.write_text(original)
            marker.symlink_to(outside)
            with patch.object(setup, "ROOT", repository.resolve()):
                try:
                    setup.prepare_root(root, "macos-aarch64")
                except ValueError as exc:
                    error = exc
                else:
                    error = None
            self.assertEqual(outside.read_text(), original)
            self.assertIsNotNone(error, "symlinked management marker must be refused")

    def test_managed_environment_drops_stale_rustc_without_mutating_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "managed"
            inherited = {"RUSTC": "/foreign/rustc", "RUSTDOC": "/foreign/rustdoc",
                         "RUSTFLAGS": "-C target-cpu=native", "CARGO_HOME": "/foreign/cargo",
                         "RUSTUP_HOME": "/foreign/rustup", "PATH": "/usr/bin"}
            with patch.dict(os.environ, inherited, clear=True):
                before = dict(os.environ)
                env = setup.environment(root, embassy=True)
                self.assertEqual(dict(os.environ), before)
                for key in ("RUSTC", "RUSTDOC", "RUSTFLAGS", "RUSTUP_TOOLCHAIN",
                            "CARGO_BUILD_TARGET"):
                    self.assertNotIn(key, env)
                self.assertEqual(env["CARGO_HOME"], str(root / "cargo"))
                self.assertEqual(env["RUSTUP_HOME"], str(root / "rustup"))
                self.assertEqual(env["PIP_CACHE_DIR"], str(root / "pip-cache"))

    def test_embassy_build_and_rust_environment_stay_inside_setup_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "isolated"
            env = setup.environment(root, embassy=True)
            command = setup.build_command("embassy", root, root / "images/embassy")
            self.assertEqual(Path(env["CARGO_HOME"]), root / "cargo")
            self.assertEqual(Path(env["RUSTUP_HOME"]), root / "rustup")
            self.assertEqual(Path(command[command.index("--cargo-target") + 1]),
                             root / "cargo-target")
            self.assertNotEqual(Path(env["CARGO_HOME"]), Path.home() / ".cargo")
            self.assertNotEqual(Path(env["RUSTUP_HOME"]), Path.home() / ".rustup")


class PythonPreparationTests(unittest.TestCase):
    def test_pip_setup_uses_binary_crypto_wheels_and_managed_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            python = root / "venv/bin/python"
            python.parent.mkdir(parents=True)
            python.write_text("fixture python")
            requirements = root / "sources/zephyr/scripts/requirements-base.txt"
            requirements.parent.mkdir(parents=True)
            requirements.write_text("# fixture requirements\n")
            calls = []

            def fake_run(argv, **kwargs):
                command = list(map(str, argv))
                calls.append((command, kwargs.get("env"), kwargs.get("capture", False)))
                return "fixture-package==1" if kwargs.get("capture") else None

            with patch.object(setup, "run", side_effect=fake_run):
                setup.prepare_python(root, ("zephyr",))

            installs = [command for command, _, _ in calls
                        if command[1:3] == ["-m", "pip"] and "install" in command]
            self.assertEqual(len(installs), 2)
            self.assertTrue(all("--only-binary=cryptography,cffi" in command
                                for command in installs))
            self.assertTrue(all(env["PIP_CACHE_DIR"] == str(root / "pip-cache")
                                for _, env, _ in calls))
            self.assertEqual(json.loads((root / ".python-platforms.json").read_text()),
                             ["zephyr"])


class EmbassyPreparationTests(unittest.TestCase):
    def test_nested_rust_src_library_is_linked_and_seeded_with_isolated_cargo(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "setup-root"
            nested_library = root / "packages/rust-src/rust-src/lib/rust/library"
            core_manifest = nested_library / "core/Cargo.toml"
            core_manifest.parent.mkdir(parents=True)
            core_manifest.write_text("[package]\nname = 'core'\n")
            (nested_library / "Cargo.toml").write_text("[workspace]\n")
            installer = root / "packages/rust/rust-dist/install.sh"
            installer.parent.mkdir(parents=True)
            installer.write_text("#!/bin/sh\n")
            (installer.parent / "components").write_text(
                "rustc\nrust-std-test-host\ncargo\nrust-docs\n"
            )
            package_paths = {
                name: root / "packages" / name
                for name in ("gcc", "rust", "rust-src", "espflash")
            }
            calls = []

            def fake_package(package_root, name, pin):
                path = package_paths[name]
                path.mkdir(parents=True, exist_ok=True)
                return path

            def fake_run(argv, cwd=setup.ROOT, env=None, capture=False):
                command = list(map(str, argv))
                calls.append((command, env, capture))
                if command[0] == "bash":
                    bin_dir = root / "rust-toolchain/bin"
                    bin_dir.mkdir(parents=True)
                    (bin_dir / "cargo").write_text("fixture cargo")
                return "mock output" if capture else None

            with patch.object(setup, "package", side_effect=fake_package), \
                 patch.object(setup, "run", side_effect=fake_run):
                setup.prepare_embassy(root, {name: {} for name in package_paths})

            linked_library = root / "rust-toolchain/lib/rustlib/src/rust/library"
            self.assertTrue(linked_library.is_symlink())
            self.assertEqual(linked_library.resolve(), nested_library.resolve())
            installer_calls = [command for command, _, _ in calls
                               if command[0] == "bash" and command[1].endswith("install.sh")]
            self.assertEqual(len(installer_calls), 1)
            self.assertIn("--components=rustc,rust-std-test-host,cargo", installer_calls[0])
            self.assertNotIn("rust-docs", installer_calls[0])
            self.assertFalse(any(command[:3] == ["rustup", "toolchain", "install"]
                                 for command, _, _ in calls))
            cargo_fetches = [command for command, _, _ in calls
                             if command[:3] == ["cargo", "+esp", "fetch"]]
            self.assertEqual(len(cargo_fetches), 3)
            rust_manifest = Path(cargo_fetches[-1][cargo_fetches[-1].index("--manifest-path") + 1])
            self.assertEqual(rust_manifest.resolve(), (nested_library / "Cargo.toml").resolve())
            self.assertTrue(all(env["CARGO_HOME"] == str(root / "cargo")
                                and env["RUSTUP_HOME"] == str(root / "rustup")
                                for _, env, _ in calls))


class BuildOutputTests(unittest.TestCase):
    def test_existing_and_symlink_outputs_are_refused_before_setup(self):
        for kind in ("directory", "live-symlink", "dangling-symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                root = base / "setup-root"
                out = base / "images"
                target = base / "outside"
                if kind == "directory":
                    out.mkdir()
                    sentinel = out / "keep"
                else:
                    out.symlink_to(target, target_is_directory=True)
                    if kind == "live-symlink":
                        target.mkdir()
                    sentinel = target / "keep"
                if kind != "dangling-symlink":
                    sentinel.write_text("user data")
                with patch.object(setup.shutil, "which", return_value="/host/tool"), \
                     patch.object(setup, "prepare_root") as prepare_root, \
                     patch.object(setup, "download") as download, \
                     patch.object(setup, "run") as run, \
                     patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    with self.assertRaises(SystemExit) as error:
                        setup.main(["--platform", "zephyr", "--build",
                                    "--root", str(root), "--out", str(out)])
                self.assertEqual(error.exception.code, 2)
                self.assertIn("fresh, non-symlink --out directory", stderr.getvalue())
                prepare_root.assert_not_called()
                download.assert_not_called()
                run.assert_not_called()
                self.assertFalse(root.exists())
                if kind != "directory":
                    self.assertTrue(out.is_symlink())
                if kind == "dangling-symlink":
                    self.assertFalse(target.exists())
                else:
                    self.assertEqual(sentinel.read_text(), "user data")

    def test_fresh_output_reaches_setup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "setup-root"
            out = Path(temporary) / "images"
            with patch.object(setup.shutil, "which", return_value="/host/tool"), \
                 patch.object(setup, "prepare_root", side_effect=ValueError("setup reached")) as prepare_root:
                with self.assertRaisesRegex(ValueError, "setup reached"):
                    setup.main(["--platform", "zephyr", "--build",
                                "--root", str(root), "--out", str(out)])
            prepare_root.assert_called_once()
            self.assertFalse(root.exists())
            self.assertFalse(out.exists())


class PlanTests(unittest.TestCase):
    def test_plan_prints_dependencies_without_creating_or_running_anything(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "planned-root"
            with patch.object(setup.urllib.request, "urlopen") as urlopen, \
                 patch.object(setup.subprocess, "run") as run, \
                 patch.object(setup, "download", wraps=setup.download) as download, \
                 patch.object(setup, "extract", wraps=setup.extract) as extract, \
                 patch.object(setup, "checkout", wraps=setup.checkout) as checkout, \
                 patch("sys.stdout", new_callable=io.StringIO) as stdout:
                setup.main(["--platform", "zephyr", "--root", str(root), "--plan"])
            self.assertIn("zephyr", stdout.getvalue().lower())
            self.assertFalse(root.exists())
            urlopen.assert_not_called()
            run.assert_not_called()
            download.assert_not_called()
            extract.assert_not_called()
            checkout.assert_not_called()


if __name__ == "__main__":
    unittest.main()
