"""Reject stale qualification inputs and misleading image comparisons."""
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("time_sync_workflow_tested", HERE / "workflow.py")
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)


class WorkflowTests(unittest.TestCase):
    def test_footprint_excludes_debug_and_separates_storage(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            firmware = out / "firmware"
            (firmware / "nuttx").mkdir(parents=True)
            (firmware / "image-size.txt").write_text(
                "section size addr\n.text 100 268435456\n.rodata 7 268435556\n"
                ".ARM.extab 9 268435563\n.ARM.exidx 8 268435572\n"
                ".data 3 939524096\n.bss 4 939524112\n.debug_info 1000000 0\n")
            (firmware / "nuttx/nuttx.bin").write_bytes(bytes(133))
            measured = workflow.footprint(out)
            self.assertEqual(measured["linked_flash_bytes"], 127)
            self.assertEqual(measured["unpadded_image_bytes"], 133)
            self.assertEqual(measured["static_ram_bytes"], 7)

    def test_stale_source_and_changed_artifact_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            record = dict(schema=1, build_complete=True, variant="controller",
                          sources_after={"source": "original"}, artifacts={"image": "original"})
            (out / "build.json").write_text(json.dumps(record))
            with patch.object(workflow, "sources", return_value={"source": "changed"}):
                with self.assertRaisesRegex(ValueError, "compiled inputs"):
                    workflow.checked_build(out)
            with patch.object(workflow, "sources", return_value=record["sources_after"]), \
                 patch.object(workflow, "validate", return_value={"image": "changed"}):
                with self.assertRaisesRegex(ValueError, "firmware"):
                    workflow.checked_build(out)

    def test_comparison_requires_common_configuration_and_compiler(self):
        baseline = dict(variant="baseline", sources_after={"source": "same"},
                        compiler={"rustc": "same"}, native_environment_sha256={},
                        identities={"nxrs": "same", "nuttx": "same", "nuttx-apps": "same"},
                        artifacts={"firmware/nuttx/.config": "same"})
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            args = types.SimpleNamespace(baseline=out / "baseline", controller=out / "controller",
                                         out=out / "comparison.json")
            for key, value in (("compiler", {"rustc": "different"}),
                               ("artifacts", {"firmware/nuttx/.config": "different"}),
                               ("sources_after", {"source": "different"})):
                controller = {**baseline, "variant": "controller", key: value}
                with patch.object(workflow, "checked_build", side_effect=[baseline, controller]):
                    with self.assertRaises(ValueError):
                        workflow.compare(args)
                self.assertFalse(args.out.exists())


if __name__ == "__main__":
    unittest.main()
