"""Scope/oracle regressions; source tests use a real prepared current SDK."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("comparison", HERE / "compare.py")
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


class ResultTests(unittest.TestCase):
    def test_failed_input_validation_removes_previous_success_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            receipt = out / "qualification.json"
            receipt.write_text('{"passed":true}\n')
            with self.assertRaises(FileNotFoundError):
                comparison.run_qemu(out, out / "unused-qemu")
            self.assertFalse(receipt.exists())

    def row(self):
        return {"schema": 1, "candidate": "upstream-queue", "id": "a" * 64,
                "case": "reservation", "passed": True, "hazard_observed": True,
                "upstream_fixed_priority_safe": False, "spins_before": 10, "spins_after": 100,
                "producer_priority": 120, "receiver_priority": 180, "coordinator_priority": 220,
                "rescue_priority": 200, "joined": 2}

    def validate(self, row, suffix=""):
        text = "STD_COMPARISON " + json.dumps(row) + "\n" + suffix
        return comparison.validate_result(text, "a" * 64, "upstream-queue", "reservation")

    def test_finite_counterexample_is_not_a_safety_pass(self):
        result = self.validate(self.row())
        self.assertTrue(result["passed"])
        self.assertFalse(result["upstream_fixed_priority_safe"])

    def test_incomplete_or_wrong_runtime_evidence_is_rejected(self):
        cases = {"id": "b" * 64, "candidate": "baseline", "schema": True,
                 "passed": False, "hazard_observed": False, "upstream_fixed_priority_safe": True,
                 "joined": 1, "producer_priority": 180, "receiver_priority": 120,
                 "coordinator_priority": 100, "rescue_priority": 110,
                 "spins_before": 0, "spins_after": 10}
        for key, value in cases.items():
            with self.subTest(key=key):
                changed = self.row()
                changed[key] = value
                with self.assertRaises(ValueError):
                    self.validate(changed)
        for suffix in ("panic after result", "STD_COMPARISON FAIL cleanup", "STD_COMPARISON " + json.dumps(self.row())):
            with self.assertRaises(ValueError):
                self.validate(self.row(), suffix)
        for field in self.row():
            changed = self.row()
            del changed[field]
            with self.assertRaises(ValueError):
                self.validate(changed)

    def test_functional_result_requires_all_controls(self):
        row = {"schema": 1, "candidate": "baseline", "id": "a" * 64,
               "case": "functional", "passed": True, "controls": 9}
        text = "PASS control\n" * 9 + "STD_COMPARISON " + json.dumps(row)
        comparison.validate_result(text, "a" * 64, "baseline", "functional")
        with self.assertRaises(ValueError):
            comparison.validate_result(text.replace("PASS control\n", "", 1), "a" * 64, "baseline", "functional")


@unittest.skipUnless(os.environ.get("NXRS_STD_COMPARISON_BASE"), "set a prepared current SDK for actual-source reversals")
class SourceTests(unittest.TestCase):
    def setUp(self):
        self.prepared = Path(os.environ["NXRS_STD_COMPARISON_BASE"]).resolve()
        self.ledger = comparison.verify_baseline(self.prepared)
        self.sdk = self.prepared / "toolchain"

    def test_all_candidates_reverse_only_the_selected_real_patches(self):
        for candidate, names in comparison.REVERSE.items():
            with self.subTest(candidate=candidate), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                selected = set()
                for name in names:
                    patch = self.prepared / name
                    selected.update(line.split("\t", 2)[2] for line in subprocess.check_output(
                        ["git", "apply", "--numstat", str(patch)], text=True).splitlines())
                for name in selected:
                    destination = root / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes((self.sdk / name).read_bytes())
                for name in names:
                    subprocess.run(["git", "apply", "--reverse", str(self.prepared / name)],
                                   cwd=root, check=True, capture_output=True)
                for entry in self.ledger["files"]:
                    if entry["source_path"] in selected:
                        path = root / entry["source_path"]
                        if entry["original_blob"] is None:
                            self.assertFalse(path.exists())
                        else:
                            self.assertEqual(comparison.blob(path), entry["original_blob"])

    def test_upstream_probe_leaves_the_algorithm_and_backoff_unchanged(self):
        source = (self.sdk / comparison.LIBRARY / comparison.ARRAY).read_text()
        instrumented = comparison.instrument_array(source)
        self.assertLess(instrumented.index("match self.tail.compare_exchange_weak"),
                        instrumented.index("__nxrs_std_compare_reserved(self"))
        self.assertLess(instrumented.index("__nxrs_std_compare_reserved(self"),
                        instrumented.index("slot.msg.get().write"))
        self.assertEqual(instrumented.count("backoff.spin_light();"), source.count("backoff.spin_light();"))
        self.assertEqual(instrumented.count("backoff.spin_heavy();"), source.count("backoff.spin_heavy();"))
        self.assertEqual(instrumented.count("__nxrs_std_compare_unpublished(self"), 1)
        with self.assertRaises(ValueError):
            comparison.instrument_array(instrumented)

    def test_pthread_candidate_initializes_only_the_native_raw_mutex(self):
        source = (self.sdk / comparison.LIBRARY / comparison.PTHREAD).read_text()
        changed = comparison.initialize_pthread_mutex(source)
        addition = '        #[cfg(target_os = "nuttx")]\n        Pin::new_unchecked(&mut (*parker).lock).init();\n'
        self.assertEqual(changed.count(addition), 1)
        self.assertEqual(changed.replace(addition, ""), source)


if __name__ == "__main__":
    unittest.main()
