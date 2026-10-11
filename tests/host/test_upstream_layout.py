"""Dependency package paths and filtered CI coverage; no compiler build."""

import importlib.util
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


def load_tool(filename):
    spec = importlib.util.spec_from_file_location(filename, ROOT / "tools" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UpstreamLayoutTests(unittest.TestCase):
    def test_version_uses_checked_release_pin_without_submodule_tags(self):
        versions = load_tool("nuttx-version.py")
        release = json.loads((ROOT / "upstream/nuttx/release.json").read_text())
        selected = subprocess.check_output(
            ["git", "rev-parse", ":external/nuttx"], cwd=ROOT, text=True).strip()
        self.assertEqual(selected, release["revision"])
        self.assertEqual(versions.release_version(selected), release["version"])
        with self.assertRaisesRegex(ValueError, "does not match the release pin"):
            versions.release_version("0" * 40)
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary).resolve()
            (archive / ".git").mkdir()
            with self.assertRaisesRegex(ValueError, "private source archive"):
                versions.stamp(archive, selected)
            (archive / ".git").rmdir()
            with self.assertRaisesRegex(ValueError, "full source revision"):
                versions.stamp(archive, "short")
            # Portable CI does not initialize NuttX. The tool's actual generated
            # version header is checked by full target builds/device uname.
            with patch.object(versions.subprocess, "run") as generate:
                self.assertEqual(versions.stamp(archive, selected), release["version"])
                generate.assert_called_once_with(
                    ["bash", str(archive / "tools/version.sh"), "-v", release["version"],
                     "-b", selected[:12] + "-nxrs", str(archive / ".version")], check=True)

    def test_archive_uses_staged_dependency_pin_and_rejects_mismatch(self):
        spec = importlib.util.spec_from_file_location(
            "rtos_build", ROOT / "tests/rtos-bench/build.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "external/nuttx"
            source.mkdir(parents=True)

            def git(directory, *args):
                return subprocess.check_output(
                    ["git", "-C", str(directory), *args], text=True,
                    stderr=subprocess.PIPE).strip()

            def commit(directory):
                git(directory, "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
                return git(directory, "rev-parse", "HEAD")

            git(root, "init", "-q")
            git(source, "init", "-q")
            payload = source / "payload"
            payload.write_text("old release\n")
            git(source, "add", "payload")
            old = commit(source)
            git(root, "update-index", "--add", "--cacheinfo",
                f"160000,{old},external/nuttx")
            commit(root)
            payload.write_text("new release\n")
            git(source, "add", "payload")
            new = commit(source)
            with patch.object(builder, "ROOT", root):
                with self.assertRaisesRegex(ValueError, "unpinned nuttx"):
                    builder.archive_submodule("nuttx", root / "rejected")
                self.assertFalse((root / "rejected").exists())
                git(root, "update-index", "--cacheinfo",
                    f"160000,{new},external/nuttx")
                self.assertEqual(builder.archive_submodule("nuttx", root / "archive"), new)
            self.assertEqual((root / "archive/payload").read_text(), "new release\n")
            self.assertEqual(git(root, "rev-parse", "HEAD:external/nuttx"), old)

    def test_builders_do_not_fall_back_to_last_committed_dependency_pins(self):
        for filename in ("tools/build-nuttx-std-app.sh", "tools/build-nuttx-sim.sh",
                         "tests/nuttx-std/build.sh", "tests/nuttx-std/userled/build.sh"):
            with self.subTest(builder=filename):
                text = (ROOT / filename).read_text()
                self.assertIn("rev-parse", text)
                self.assertNotIn("HEAD:external/", text)
                self.assertIn(":external/", text)
                self.assertIn("nxrs_dirty=", text)
                if filename != "tests/nuttx-std/userled/build.sh":
                    self.assertIn("tools/nuttx-version.py", text)

    def test_dependency_packages_leave_platform_integration_in_place(self):
        for component in ("nuttx", "nuttx-apps", "rust-llvm", "rust-std"):
            with self.subTest(component=component):
                self.assertTrue((ROOT / "upstream" / component).is_dir())
                old = ROOT / "platform" / component
                if component == "nuttx":
                    self.assertFalse((old / "patches").exists())
                    for name in ("platforms", "profiles", "qualification", "std-app"):
                        self.assertTrue((old / name).is_dir())
                else:
                    self.assertFalse(old.exists())

    def test_applicators_and_builder_resolve_the_dependency_packages(self):
        patches = load_tool("apply-nuttx-patches.py")
        llvm = ROOT / "upstream/rust-llvm"
        self.assertEqual(patches.PROPOSAL_MANIFEST, llvm / "proposals/series.json")
        self.assertEqual(patches.PINNED_METADATA["rust-llvm"], llvm / "upstream.json")
        for component, series in patches.SERIES.items():
            for name in series:
                with self.subTest(component=component, patch=name):
                    self.assertTrue((ROOT / "upstream" / component / "patches" / name).is_file())
        manifest = json.loads(patches.PROPOSAL_MANIFEST.read_text())
        for selection in manifest["sets"]:
            names, _ = patches.proposal_selection("rust-llvm", selection, None)
            for name in names:
                self.assertTrue((llvm / "proposals" / name).is_file(), name)
        std = load_tool("apply-rust-std-proposals.py")
        self.assertEqual(std.PROPOSAL_DIR, ROOT / "upstream/rust-std/proposals")
        self.assertTrue(std.PROPOSAL_MANIFEST.is_file())
        builder = load_tool("build-rust-llvm.py")
        self.assertEqual(builder.PINS, json.loads((llvm / "upstream.json").read_text()))

    def test_filtered_ci_events_include_both_nuttx_patchsets(self):
        # Inspect push and PR independently: a correct push filter must not
        # conceal a stale PR filter (or vice versa).
        events = re.compile(r"^  (push|pull_request):\s*\n(.*?)(?=^  [\w-]+:|^\S|\Z)",
                            re.MULTILINE | re.DOTALL)
        checked = 0
        for workflow in sorted((ROOT / ".github/workflows").glob("*.yml")):
            for event, body in events.findall(workflow.read_text()):
                if "    paths:" not in body or "tools/apply-nuttx-patches.py" not in body:
                    continue
                with self.subTest(workflow=workflow.name, event=event):
                    for component in ("nuttx", "nuttx-apps"):
                        directory = "nuttx/**" if component == "nuttx" else "nuttx-apps/patches/**"
                        self.assertIn(f"'upstream/{directory}'", body)
                    self.assertIn("'tools/nuttx-version.py'", body)
                checked += 1
        self.assertGreater(checked, 0)

    def test_local_compiler_action_can_resolve_the_shared_builder(self):
        action_dir = ROOT / "upstream/rust-llvm"
        path = re.search(r'"\$GITHUB_ACTION_PATH/([^"\n]+)"',
                         (action_dir / "action.yml").read_text())
        self.assertIsNotNone(path)
        self.assertEqual((action_dir / path.group(1)).resolve(),
                         ROOT / "tools/build-rust-llvm.py")


if __name__ == "__main__":
    unittest.main()
