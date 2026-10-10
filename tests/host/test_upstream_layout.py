"""Dependency package paths and filtered CI coverage; no compiler build."""

import importlib.util
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]


def load_tool(filename):
    spec = importlib.util.spec_from_file_location(filename, ROOT / "tools" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UpstreamLayoutTests(unittest.TestCase):
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
                        self.assertIn(f"'upstream/{component}/patches/**'", body)
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
