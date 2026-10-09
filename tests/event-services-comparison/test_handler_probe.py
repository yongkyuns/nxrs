import hashlib
import json
import importlib.util
from pathlib import Path
from statistics import median
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("handler_probe", HERE / "handler_probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def capture():
    rows = []
    for sequence in range(39):
        for peer in range(3):
            index = sequence * 3 + peer
            running, other, irq = 1000 + index, 200 + peer, 30
            wall = running + other + irq
            rows.append(
                f"HP_ROW sequence={sequence} peer={peer} wall_cycles={wall} "
                f"running_cycles={running} other_cycles={other} irq_cycles={irq} "
                f"switches={index % 4} observed_cycles={wall + 3}")
    return "\n".join([*rows, "HP_DONE jobs=117 errors=0"])


class HandlerProbeTests(unittest.TestCase):
    def test_cli_adapt_outputs_api_equivalent_sources_and_refuses_existing_directory(self):
        runtime_source = (HERE / "runtime.c").read_text()
        platform_source = (HERE / "platform_nuttx.c").read_text()
        expected_runtime, expected_platform = probe.adapt_diagnostic_sources(
            runtime_source, platform_source)
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "diagnostic-copy"
            result = subprocess.run(
                [sys.executable, str(HERE / "handler_probe.py"), "adapt", "--out", str(out)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((out / "runtime.c").read_text(), expected_runtime)
            self.assertEqual((out / "platform_nuttx.c").read_text(), expected_platform)

            sentinel = out / "keep.txt"
            sentinel.write_text("untouched")
            refused = subprocess.run(
                [sys.executable, str(HERE / "handler_probe.py"), "adapt", "--out", str(out)],
                capture_output=True, text=True)
            self.assertEqual(refused.returncode, 2)
            self.assertIn("output path already exists", refused.stderr)
            self.assertEqual(sentinel.read_text(), "untouched")

    def test_cli_parse_emits_json_or_a_clean_error(self):
        with tempfile.TemporaryDirectory() as temp:
            capture_path = Path(temp) / "capture.txt"
            capture_path.write_text(capture())
            valid = subprocess.run(
                [sys.executable, str(HERE / "handler_probe.py"), "parse", str(capture_path)],
                capture_output=True, text=True)
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertEqual(json.loads(valid.stdout), probe.parse_rows(capture()))

            skewed_lines = capture().splitlines()
            skewed_lines[0] = skewed_lines[0].replace("observed_cycles=1233",
                                                      "observed_cycles=1996")
            capture_path.write_text("\n".join(skewed_lines))
            default_bound = subprocess.run(
                [sys.executable, str(HERE / "handler_probe.py"), "parse", str(capture_path)],
                capture_output=True, text=True)
            self.assertEqual(default_bound.returncode, 2)
            self.assertIn("snapshot skew exceeds", default_bound.stderr)
            expanded_bound = subprocess.run(
                [sys.executable, str(HERE / "handler_probe.py"), "parse",
                 "--max-snapshot-skew", "1024", str(capture_path)],
                capture_output=True, text=True)
            self.assertEqual(expanded_bound.returncode, 0, expanded_bound.stderr)
            self.assertEqual(json.loads(expanded_bound.stdout)["rows"][0]["observed_cycles"],
                             1996)
            over_cap = subprocess.run(
                [sys.executable, str(HERE / "handler_probe.py"), "parse",
                 "--max-snapshot-skew", "4097", str(capture_path)],
                capture_output=True, text=True)
            self.assertEqual(over_cap.returncode, 2)
            self.assertIn("between 0 and 4096", over_cap.stderr)

            capture_path.write_text("\n".join(capture().splitlines()[:-1]))
            invalid = subprocess.run(
                [sys.executable, str(HERE / "handler_probe.py"), "parse", str(capture_path)],
                capture_output=True, text=True)
            self.assertEqual(invalid.returncode, 2)
            self.assertEqual(invalid.stdout, "")
            self.assertIn("expected exactly one complete HP_DONE", invalid.stderr)
            self.assertNotIn("Traceback", invalid.stderr)

    def test_diagnostic_adapter_renames_hooks_without_changing_portable_loop(self):
        runtime_source = (HERE / "runtime.c").read_text()
        platform_source = (HERE / "platform_nuttx.c").read_text()
        adapted_runtime, adapted_platform = probe.adapt_diagnostic_sources(
            runtime_source, platform_source)

        self.assertTrue(adapted_runtime.startswith(probe.RUNTIME_PROTOTYPES))
        self.assertIn("uint32_t es_diag_original_now(void) {", adapted_runtime)
        self.assertIn("void es_diag_original_receive(", adapted_runtime)
        self.assertIn("void es_diag_original_resources(void) {", adapted_platform)
        self.assertEqual(adapted_runtime.count("es_probe_register(id);"), 1)
        self.assertEqual(adapted_runtime.count("es_probe_reset();"), 1)
        self.assertIn("uint32_t started = es_now();", adapted_runtime)
        self.assertIn("es_record_receive(id, &event, result, started, finished);",
                      adapted_runtime)

        def service_loop(source):
            start = source.index("static void service_loop(unsigned id) {")
            end = source.index("\n}\n#else", start) + 2
            return source[start:end]

        self.assertEqual(service_loop(adapted_runtime), service_loop(runtime_source))

    def test_diagnostic_adapter_refuses_missing_duplicate_and_reapplied_anchors(self):
        runtime_source = (HERE / "runtime.c").read_text()
        platform_source = (HERE / "platform_nuttx.c").read_text()
        with self.assertRaisesRegex(ValueError, "exactly one es_now"):
            probe.adapt_diagnostic_sources(
                runtime_source.replace("uint32_t es_now(void) {", "", 1), platform_source)
        with self.assertRaisesRegex(ValueError, "exactly one es_now"):
            probe.adapt_diagnostic_sources(
                runtime_source + "\nuint32_t es_now(void) {\n", platform_source)
        adapted_runtime, adapted_platform = probe.adapt_diagnostic_sources(
            runtime_source, platform_source)
        with self.assertRaisesRegex(ValueError, "already adapted"):
            probe.adapt_diagnostic_sources(adapted_runtime, adapted_platform)
        with self.assertRaisesRegex(ValueError, "exactly one es_platform_resources"):
            probe.adapt_diagnostic_sources(runtime_source,
                platform_source.replace("void es_platform_resources(void) {", "", 1))

    def test_validates_full_capture_and_summarizes_disjoint_categories(self):
        result = probe.parse_rows(capture())
        self.assertTrue(result["diagnostic_only"])
        self.assertEqual(result["work_iterations"], 400000)
        self.assertEqual((result["jobs"], result["errors"]), (117, 0))
        self.assertEqual(len(result["rows"]), 117)
        totals = result["summary"]["totals"]
        self.assertEqual(totals["wall_cycles"], totals["running_cycles"]
                         + totals["other_cycles"] + totals["irq_cycles"])
        max_row = result["summary"]["max_wall_row"]
        self.assertEqual(max_row["wall_cycles"], max(
            row["wall_cycles"] for row in result["rows"]))
        self.assertEqual(max_row["wall_cycles"], max_row["running_cycles"]
                         + max_row["other_cycles"] + max_row["irq_cycles"])
        self.assertIn("not instruction-only", result["interpretation"])
        self.assertIn("not production qualification", result["interpretation"])
        self.assertIn("proof of preemption", result["interpretation"])

    def test_rejects_duplicate_identity_even_with_full_row_count(self):
        lines = capture().splitlines()
        lines[1] = lines[0]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            probe.parse_rows("\n".join(lines))

    def test_rejects_inconsistent_categories_and_excess_snapshot_skew(self):
        output = capture()
        lines = output.splitlines()
        lines[0] = lines[0].replace("wall_cycles=1230", "wall_cycles=1231")
        with self.assertRaisesRegex(ValueError, "category sum"):
            probe.parse_rows("\n".join(lines))
        lines = output.splitlines()
        lines[0] = lines[0].replace("observed_cycles=1233", "observed_cycles=1500")
        with self.assertRaisesRegex(ValueError, "snapshot skew"):
            probe.parse_rows("\n".join(lines))
        self.assertEqual(len(probe.parse_rows("\n".join(lines),
                                              max_snapshot_skew=300)["rows"]), 117)

    def test_rejects_bad_completion_count_errors_and_schema(self):
        with self.assertRaisesRegex(ValueError, "completed jobs"):
            probe.parse_rows(capture().replace("jobs=117", "jobs=116"))
        with self.assertRaisesRegex(ValueError, "application errors"):
            probe.parse_rows(capture().replace("errors=0", "errors=1"))
        with self.assertRaisesRegex(ValueError, "117 rows"):
            probe.parse_rows("\n".join(capture().splitlines()[:-2] + ["HP_DONE jobs=117 errors=0"]))
        with self.assertRaisesRegex(ValueError, "schema"):
            probe.parse_rows(capture().replace(" switches=", " switch_count=", 1))

    def test_rejects_negative_counters_and_missing_completion_marker(self):
        lines = capture().splitlines()
        lines[0] = lines[0].replace("running_cycles=1000", "running_cycles=-1")
        with self.assertRaisesRegex(ValueError, "unsigned 32-bit"):
            probe.parse_rows("\n".join(lines))
        with self.assertRaisesRegex(ValueError, "HP_DONE"):
            probe.parse_rows("\n".join(capture().splitlines()[:-1]))

    def test_snapshot_skew_bound_is_configurable_but_capped(self):
        with self.assertRaisesRegex(ValueError, "between 0 and"):
            probe.parse_rows(capture(), max_snapshot_skew=4097)

    def test_frozen_alignment_evidence_revalidates_every_numeric_row(self):
        report = json.loads((HERE / "results/esp32s3-handler-alignment-2026-10-05.json").read_text())
        self.assertTrue(report["diagnostic_only"])
        self.assertEqual(report["cpu_hz"], 240000000)
        self.assertEqual(len(report["cohorts"]), 2)
        medians = self.validate_frozen_rows(report)
        self.assertEqual(medians, [dict(c=2405920, rust=2807167),
                                   dict(c=2405919, rust=2406496)])
        native, assembled = report["cohorts"]
        self.assertTrue(native["builds"]["rust"]["loops"][-1]["first_crosses_word"])
        self.assertFalse(assembled["builds"]["rust"]["loops"][-1]["first_crosses_word"])

    def validate_frozen_rows(self, report):
        """Reparse every stored row; historical and driver cohorts stay separate."""
        c_images, kernels = set(), set()
        medians = []
        for cohort in report["cohorts"]:
            self.assertTrue(cohort["restore_verified"])
            self.assertEqual(len(cohort["runs"]), 8)
            c_images.add(cohort["builds"]["c"]["artifacts"]["image.bin"])
            kernels.update(b["kernel_objects_sha256"] for b in cohort["builds"].values())
            values = {case: [] for case in ("c", "rust")}
            for run in cohort["runs"]:
                lines = ["HP_ROW " + " ".join(f"{key}={value}" for key, value in
                         zip(report["row_fields"], row, strict=True)) for row in run["rows"]]
                parsed = probe.parse_rows("\n".join([*lines, "HP_DONE jobs=117 errors=0"]),
                                          max_snapshot_skew=cohort["snapshot_skew_limit_cycles"])
                self.assertEqual(parsed["summary"], run["summary"])
                self.assertEqual((run["result"]["accepted"], run["result"]["received"]),
                                 (3900, 3900))
                self.assertEqual((run["result"]["rejected"], run["result"]["errors"]), (0, 0))
                values[run["case"]].extend(row["running_cycles"] for row in parsed["rows"])
            medians.append({case: median(rows) for case, rows in values.items()})
        self.assertEqual((len(c_images), len(kernels)), (1, 1))
        return medians

    def test_rebuilt_driver_evidence_binds_patch_and_revalidates_rows(self):
        path = HERE / "results/esp32s3-handler-rust-driver-2026-10-05.json"
        text = path.read_text()
        report = json.loads(text)
        self.assertEqual(len(report["cohorts"]), 1)
        self.assertEqual(self.validate_frozen_rows(report),
                         [dict(c=2405917, rust=2406451.5)])
        cohort = report["cohorts"][0]
        historical = json.loads((HERE / "results/esp32s3-handler-alignment-2026-10-05.json").read_text())
        self.assertEqual(cohort["builds"]["c"]["artifacts"],
                         historical["cohorts"][0]["builds"]["c"]["artifacts"])
        self.assertEqual(cohort["builds"]["rust"]["loops"][-1]["first_opcode"], "ssai")
        for loop in cohort["builds"]["rust"]["loops"]:
            self.assertFalse(loop["first_crosses_word"])
            self.assertEqual(int(loop["body_pc"], 16) % 4, 0)
        evidence = report["compiler_evaluation"]
        self.assertEqual(evidence["backend_tests"], 102)
        root = HERE.parents[1]
        patches = evidence["patch_ledger"]["patches"]
        self.assertEqual(len(patches), 7)
        self.assertTrue(patches[-1]["proposal"])
        for entry in patches:
            folder = "proposals" if entry.get("proposal") else "patches"
            actual = root / "upstream/rust-llvm" / folder / entry["name"]
            self.assertEqual(entry["sha256"], hashlib.sha256(actual.read_bytes()).hexdigest())
        for private in ('"raw_output"', "/Users/", "/home/", "usbmodem", "device-before.bin"):
            self.assertNotIn(private, text)


if __name__ == "__main__":
    unittest.main()
