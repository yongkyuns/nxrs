"""Negative controls for the supplemental, original-receipt evidence gate."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("time_supplemental_verifier", HERE / "verify.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def fixtures():
    builds, receipts = [], []
    for feature in ([], ["controller"]):
        builds.append({"compiler": {"rustc_sha256": "rustc", "cc_sha256": "cc"},
                       "identities": {"nuttx": "nuttx", "nuttx-apps": "apps"},
                       "artifacts": {"firmware/nuttx/nuttx": "elf", "firmware/nuttx/nuttx.bin": "binary",
                                     "firmware/nuttx/.config": "config"}})
        receipts.append({"schema": 1, "inputs": {"features": feature, "controller": "frontend",
                         "compiler": "rustc", "native_compiler": "cc", "nuttx": "nuttx", "apps": "apps",
                         "build_inputs": {"source": "hash"}, "sdk_inputs": {"source": "hash"}},
                         "files": {"nuttx/nuttx:bytes": "elf", "nuttx/nuttx.bin:bytes": "binary",
                                   "nuttx/.config:bytes": "config"}})
    return builds, receipts, ["frontend", "frontend"]


class EvidenceTests(unittest.TestCase):
    def test_original_receipts_allow_only_feature_difference(self):
        result = verifier.receipt_pair(*fixtures())
        self.assertTrue(result["same_inputs_except_features"])
        self.assertEqual(result["expected_features"], [[], ["controller"]])
        for key, value in (("features", []), ("controller", "other"),
                           ("build_inputs", {"source": "changed"}),
                           ("sdk_inputs", {"source": "changed"}), ("unrecorded", "changed")):
            with self.subTest(key=key):
                builds, receipts, frontends = fixtures()
                receipts[1]["inputs"][key] = value
                with self.assertRaises(ValueError):
                    verifier.receipt_pair(builds, receipts, frontends)

    def test_receipts_are_bound_to_compilers_and_actual_images(self):
        for key in ("nuttx/nuttx:bytes", "nuttx/nuttx.bin:bytes", "nuttx/.config:bytes"):
            builds, receipts, frontends = fixtures()
            receipts[1]["files"][key] = "different"
            with self.assertRaises(ValueError):
                verifier.receipt_pair(builds, receipts, frontends)
        builds, receipts, frontends = fixtures()
        frontends[1] = "not-the-copied-frontend"
        with self.assertRaises(ValueError):
            verifier.receipt_pair(builds, receipts, frontends)
        for key in ("rustc_sha256", "cc_sha256"):
            builds, receipts, frontends = fixtures()
            builds[1]["compiler"][key] = "different"
            with self.assertRaises(ValueError):
                verifier.receipt_pair(builds, receipts, frontends)

    def test_projection_preserves_every_input_and_feature(self):
        _, receipts, _ = fixtures()
        inputs = receipts[1]["inputs"]
        common = {key: value for key, value in inputs.items() if key != "features"}
        projection = {"schema": 1, "kind": "original-native-receipt-input-projection",
                      "features": ["controller"], "canonical_inputs_sha256": verifier.canonical_hash(inputs),
                      "selected_file_hashes": receipts[1]["files"]}
        self.assertEqual(verifier.restore_receipt(common, projection), receipts[1])
        changed = copy.deepcopy(common)
        changed["sdk_inputs"]["source"] = "different"
        with self.assertRaises(ValueError):
            verifier.restore_receipt(changed, projection)
        with self.assertRaises(ValueError):
            verifier.restore_receipt(common, {**projection, "features": []})

    def test_retained_capture_replay(self):
        evidence = HERE.parent / "results/mps2-100us"
        if not evidence.exists():
            self.skipTest("retained capture is not present yet")
        self.assertTrue(verifier.verify(evidence)["qualified"])


if __name__ == "__main__":
    unittest.main()
