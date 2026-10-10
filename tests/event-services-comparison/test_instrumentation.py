"""Lean builds remove distributions, not delivery checks or execution resources."""
from pathlib import Path
import copy
import hashlib
import json
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_controls import control, matrix, report, traffic_output, saturation_output

HERE = Path(__file__).resolve().parent


def lean_output():
    text = traffic_output(control_edits={"diagnostic_bytes": 20})
    text = re.sub(r"([a-z_]*p99_us)=\d+", r"\1=0", text)
    return "ES_INSTRUMENTATION mode=lean histogram_bins=0\nES_SCHED diagnostic_bytes=20\n" + text


class InstrumentationTests(unittest.TestCase):
    def test_public_rebuilt_pairs_preserve_firmware_and_qualify_all_runs(self):
        record = json.loads((HERE / "results/esp32s3-instrumentation-2026-10-10.json").read_text())
        report.instrumentation_report(record["reference"], record["lean"])
        runs = 0
        for name, before in record["reference"]["cases"].items():
            after = record["lean"]["cases"][name]
            self.assertEqual(before["flash_and_ram"]["whole_ram_peak_bytes"] -
                             after["flash_and_ram"]["whole_ram_peak_bytes"], 26112)
            for case in (before, after):
                for profile in case["profiles"].values():
                    self.assertEqual(profile["run_count"], profile["qualified_run_count"])
                    runs += profile["run_count"]
                for source, digest in case["source_sha256"].items():
                    if not source.endswith(".py"):
                        self.assertEqual(hashlib.sha256((HERE / source).read_bytes()).hexdigest(), digest)
        self.assertEqual(runs, 48)
        for profile in ("normal", "burst"):
            for case in record["lean"]["cases"].values():
                for run in case["profiles"][profile]["runs"]:
                    for row in (run["result"], run["control"], *run["services"]):
                        self.assertFalse(any(key.endswith("_us") for key in row))

    def test_live_capture_and_record_replay_use_the_same_storage_accounting(self):
        for mode, size in (("full", 276), ("lean", 20)):
            text = lean_output() if mode == "lean" else (
                "ES_INSTRUMENTATION mode=full histogram_bins=64\n"
                "ES_SCHED diagnostic_bytes=276\n" + traffic_output())
            with self.subTest(mode=mode), patch.object(control.measure.os, "write"), \
                 patch.object(control.measure, "read_until_completion", return_value=text.encode()), \
                 patch.object(control.measure, "capture_free_memory", return_value=(b"", None)):
                row = control.run_one(73, "normal", "one", "embassy", instrumentation=mode)
                record = {"instrumentation": mode, "contract_version": 1, "failure": None,
                          "completed_runs": 1, "requested_runs": 1, "image_sha256": "a" * 64,
                          "runs": [row]}
                checked = control.verify_record(record, image_sha256="a" * 64,
                    platform="embassy", layout="one", profiles=["normal"], runs=1,
                    instrumentation=mode)
                self.assertEqual(checked[0]["memory"]["scheduling_diagnostics"], size)

    def test_comparison_requires_matched_rebuilds_and_complete_qualification(self):
        resources = {k: {"median": 1, "max": 1} for k in
            ("queue_buffers", "queue_objects", "thread_objects", "entry_storage", "stack_storage")}
        full = {"profiles": ["normal", "burst", "saturation"], "blocks": 1,
                "runs_per_profile": 1, "backup_sha256": "a" * 64, "cases": {}}
        for platform in report.PLATFORMS:
            full["cases"][platform + "-three"] = {
                "configuration": {"instrumentation": "full", "slots": 480},
                "kernel_config_identity": "b" * 64,
                "source_sha256": {"core.rs": "c" * 64, "build.py": "d" * 64},
                "profiles": {p: {"run_count": 1, "qualified_run_count": 1,
                                 "resource_metrics": copy.deepcopy(resources)} for p in full["profiles"]}}
            full["cases"][platform + "-three"]["profiles"]["saturation"]["runs"] = [
                {"resources": [{key: value["max"] for key, value in resources.items()}]}]
        lean = copy.deepcopy(full)
        for case in lean["cases"].values():
            case["configuration"]["instrumentation"] = "lean"
        self.assertEqual(report.instrumentation_report(full, lean)["lean"], lean)
        for key in ("configuration", "source_sha256", "kernel_config_identity", "qualification", "resources"):
            damaged = copy.deepcopy(lean)
            case = damaged["cases"]["nuttx-rust-three"]
            if key == "configuration": case[key]["slots"] = 240
            elif key == "source_sha256": case[key]["core.rs"] = "0" * 64
            elif key == "kernel_config_identity": case[key] = "0" * 64
            elif key == "qualification": case["profiles"]["normal"]["qualified_run_count"] = 0
            else: case["profiles"]["normal"]["resource_metrics"]["stack_storage"]["max"] = 2
            with self.subTest(key=key), self.assertRaises(ValueError):
                report.instrumentation_report(full, damaged)

    def test_comparison_rejects_saturation_resource_changes(self):
        record = json.loads((HERE / "results/esp32s3-instrumentation-2026-10-10.json").read_text())
        for name in record["lean"]["cases"]:
            for resource in ("queue_buffers", "queue_objects", "thread_objects", "entry_storage", "stack_storage"):
                damaged = copy.deepcopy(record["lean"])
                samples = damaged["cases"][name]["profiles"]["saturation"]["runs"][-1]["resources"]
                samples[-1][resource] += 16
                with self.subTest(case=name, resource=resource), self.assertRaisesRegex(
                        ValueError, "saturation execution/queue resources"):
                    report.instrumentation_report(record["reference"], damaged)
        for runs in ([], [{"resources": []}]):
            damaged = copy.deepcopy(record)
            for cohort in ("reference", "lean"):
                damaged[cohort]["cases"]["nuttx-c-three"]["profiles"]["saturation"]["runs"] = runs
            with self.subTest(runs=runs), self.assertRaisesRegex(ValueError, "saturation execution/queue resources"):
                report.instrumentation_report(damaged["reference"], damaged["lean"])

    def test_saturation_resource_guard_excludes_telemetry_and_heap_observations(self):
        record = json.loads((HERE / "results/esp32s3-instrumentation-2026-10-10.json").read_text())
        for case in record["lean"]["cases"].values():
            for run in case["profiles"]["saturation"]["runs"]:
                for sample in run["resources"]:
                    sample["heap_allocated"] += 16
                    sample["diagnostics_queue_peaks"] += 4
                    sample["note"] = "not a queue or execution reservation"
        report.instrumentation_report(record["reference"], record["lean"])

    def test_c_and_rust_storage_and_scalar_checks_match_in_both_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            for lean in (False, True):
                size, diagnostics = (8, 216) if lean else (264, 1496)
                c = f'''
#include "runtime.h"
_Static_assert(sizeof(es_distribution) == {size}, "distribution");
_Static_assert(sizeof(struct es_diagnostics) == {diagnostics}, "diagnostics");
int main(void) {{
  es_distribution a = {{0}}, b = {{0}};
  es_distribution_add(&a, 13); es_distribution_add(&b, 17);
  es_distribution_merge(&a, &b);
  return a.count != 2 || a.maximum != 17 ||
    es_distribution_percentile(&a, 100) != {0 if lean else 17};
}}
'''
                executable = Path(temporary) / ("lean-c" if lean else "full-c")
                subprocess.run(["cc", "-std=c11", "-Wall", "-Werror", "-I", str(HERE),
                    f"-DES_LEAN={int(lean)}", "-x", "c", "-", str(HERE / "core.c"),
                    "-o", str(executable)], input=c, text=True, check=True, capture_output=True)
                subprocess.run([executable], check=True)
                rust = f'''
#[path = "{HERE / 'core.rs'}"] mod protocol;
#[path = "{HERE / 'telemetry.rs'}"] mod telemetry;
fn main() {{
  assert_eq!(std::mem::size_of::<telemetry::Histogram>(), {size});
  assert_eq!(std::mem::size_of::<telemetry::Diagnostics>(), {diagnostics});
  assert_eq!(std::mem::size_of::<protocol::State>(), 152);
  let mut a = telemetry::EMPTY_HIST;
  let mut b = telemetry::EMPTY_HIST;
  a.add(13); b.add(17); telemetry::merge(&mut a, &b);
  assert_eq!((a.count, a.maximum, a.percentile(100)), (2, 17, {0 if lean else 17}));
  let mut d = telemetry::Diagnostics::new();
  let e = protocol::Event::new(0, 0, &protocol::Release {{
    sequence: 1, kind: 1, count: 1, scheduled_cycles: 0 }}, 0);
  d.send(&e, 0, 240);
  d.send(&e, 1, 480);
  assert_eq!((d.attempted, d.rejected, d.accepted[1].count), (2, 1, 1));
  assert_eq!(d.accepted[1].last_sequence, 1);
  d.receive(&e, false, 0, 0);
  assert_eq!(d.errors, 1);
}}
'''
                executable = Path(temporary) / ("lean-rust" if lean else "full-rust")
                args = ["rustc", "+1.90.0", "--edition=2024", "-Adead_code", "-", "-o", str(executable)]
                if lean:
                    args += ["--cfg", 'feature="lean"']
                subprocess.run(args, input=rust, text=True, check=True, capture_output=True)
                subprocess.run([executable], check=True)

    def test_lean_latency_is_never_exported_or_accepted_by_timing_parser(self):
        text = lean_output()
        with self.assertRaisesRegex(ValueError, "instrumentation mismatch"):
            control.validate_traffic_output(text, "normal", "one", "embassy")
        parsed = control.validate_traffic_output(text, "normal", "one", "embassy", "lean")
        for row in [parsed["result"], parsed["control"], *parsed["services"]]:
            self.assertFalse(any(key.endswith("_us") for key in row))
        self.assertEqual(parsed["result"]["received"], 3900)

    def test_lean_mode_requires_marker_and_still_rejects_bad_delivery(self):
        for text in (traffic_output(), lean_output().replace("received=3900", "received=3899"),
                     lean_output().replace("start_p99_us=0", "start_p99_us=1")):
            with self.assertRaises(ValueError):
                control.validate_traffic_output(text, "normal", "one", "embassy", "lean")

    def test_lean_saturation_still_qualifies_every_slot_and_overflow(self):
        prefix = "ES_INSTRUMENTATION mode=lean histogram_bins=0\n"
        parsed = control.validate_saturation_output(prefix + saturation_output(), "embassy", "one", "lean")
        self.assertEqual(sum(c["filled"] for c in parsed["cycles"]), 1440)
        with self.assertRaises(ValueError):
            control.validate_saturation_output(prefix + saturation_output().replace("filled=480", "filled=479"), "embassy", "one", "lean")

    def test_matrix_command_preserves_instrumentation_identity(self):
        args = matrix.command("nuttx-c-three", "image", "out", "port", "tool", 1, ["normal"], "event", "lean")
        self.assertEqual(args[args.index("--instrumentation") + 1], "lean")
