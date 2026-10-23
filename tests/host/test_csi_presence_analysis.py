"""Offline evidence gates; no hardware, vendor downloads or accuracy claims."""

import importlib.util
import json
import math
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


POLICY = load("policy_witness", "tests/csi-radio/presence/policy.py")
ANALYSIS = load("policy_analysis", "tests/csi-radio/presence/analyze.py")


def sample(value=0.01, **overrides):
    result = dict(
        host_elapsed_ms=1000,
        ms=900,
        state="presence_hint",
        phase="empty_observe",
        wander=value,
        jitter=0.001,
        average=value,
        threshold=0.001 / 8.25,
        training=0,
        ready=1,
        calibrated=1,
        advisory=1,
        rf_freshness="not_exported",
    )
    result.update(overrides)
    return result


class SelectedVendorPolicyTests(unittest.TestCase):
    def test_extracts_definition_without_changing_comments_or_literals(self):
        source = """static float f(int n);
static float f(int n) {
    /* } */ const char *s = "{";
    if (n) { return 1; } // }
    return s[0] == '{';
}
static int other(void) { return 0; }
"""
        selected = POLICY.function(source, "f")
        self.assertTrue(selected.startswith("static float f(int n) {"))
        self.assertIn('/* } */ const char *s = "{";', selected)
        self.assertNotIn("other", selected)
        self.assertTrue(
            source.endswith(selected + "\nstatic int other(void) { return 0; }\n")
        )

    def test_incomplete_missing_or_duplicate_functions_are_rejected(self):
        for source in (
            "",
            "int f(void);",
            "int f(void) {",
            "int f(void) {}\nint f(void) {}",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                POLICY.function(source, "f")

    def test_only_one_simple_define_is_accepted(self):
        self.assertEqual(
            POLICY.define("#define LIMIT 25U /* history */\n", "LIMIT"),
            "#define LIMIT 25U /* history */",
        )
        for source in (
            "",
            "#define LIMIT 25\n#define LIMIT 26",
            "#define LIMIT \\\n25",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                POLICY.define(source, "LIMIT")

    def test_package_digest_is_checked_before_archive_parsing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.zip"
            path.write_bytes(b"not a vendor package")
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                POLICY.package_source(path, "esp-radar")

    def test_native_witness_requires_all_results_and_negative_control(self):
        output = "\n".join(
            [
                "POLICY_VALUE stationary_default_present 1",
                "POLICY_VALUE stationary_minimum_present 0",
                "POLICY_VALUE above_baseline_minimum_present 1",
                "POLICY_VALUE collected_template_events 44",
                "POLICY_VALUE retained_template_slots 10",
                "POLICY_REPORT checks=30 failures=0",
                "",
            ]
        )
        POLICY.verify(output, 0)
        POLICY.verify("POLICY_DELIBERATE_FAILURE\n", 1, True)
        for altered in (
            "",
            output + output,
            output.replace("checks=30", "checks=29"),
            output.replace(
                "stationary_default_present 1", "stationary_default_present 0"
            ),
            output.replace("retained_template_slots 10", "retained_template_slots 44"),
            output + "POLICY_FAIL fake\n",
        ):
            with self.subTest(output=altered), self.assertRaises(ValueError):
                POLICY.verify(altered, 0)
        for status in (0, 2):
            with self.assertRaises(ValueError):
                POLICY.verify("POLICY_DELIBERATE_FAILURE\n", status, True)
        with self.assertRaises(ValueError):
            POLICY.verify(output, 1, True)


class DiagnosticAnalysisTests(unittest.TestCase):
    def test_summary_cannot_export_private_extra_fields(self):
        phases = {
            name: [sample(private_network="must-stay-private")]
            for name in ANALYSIS.PHASES
        }
        result = ANALYSIS.summarize(phases, 0.001, 0.25)
        self.assertNotIn("must-stay-private", json.dumps(result))
        self.assertNotIn("private_network", json.dumps(result))

    def test_rounding_intervals_do_not_claim_borderline_classifications(self):
        self.assertIsNone(ANALYSIS.comparison(0.01, 0.01))
        self.assertIsNone(ANALYSIS.comparison(0.010000005, 0.01))
        self.assertIs(ANALYSIS.comparison(0.0101, 0.01), True)
        self.assertIs(ANALYSIS.comparison(0.0099, 0.01), False)

    def test_invalid_stale_or_unqualified_sample_cannot_enter_analysis(self):
        ANALYSIS.validate([sample()])
        for records in (
            [],
            [sample(), sample()],
            [sample(), sample(ms=800, host_elapsed_ms=1001)],
            [sample(wander=math.nan)],
            [sample(average=-0.1)],
            [sample(calibrated=0)],
            [sample(advisory=0)],
            [sample(rf_freshness="fresh")],
            [sample(state="unknown")],
        ):
            with self.subTest(records=records), self.assertRaises(ValueError):
                ANALYSIS.validate(records)

    def test_confirmed_phase_boundaries_exclude_transition_wait(self):
        labels = {"transitions": [{"phase": "empty_observe", "host_elapsed_ms": 1000}]}
        instructions = [
            {"phase_before_instruction": "empty_observe", "host_elapsed_ms": 6000}
        ]
        selected = ANALYSIS.retained(
            [sample(host_elapsed_ms=x) for x in (2999, 3000, 5000, 5001)],
            labels,
            instructions,
            "empty_observe",
        )
        self.assertEqual([x["host_elapsed_ms"] for x in selected], [3000, 5000])
        with self.assertRaises(ValueError):
            ANALYSIS.retained([sample()], labels, [], "empty_observe")

    def test_all_supported_sensitivities_fail_when_empty_exceeds_baseline(self):
        phases = {name: [sample(0.01)] for name in ANALYSIS.PHASES}
        result = ANALYSIS.summarize(phases, 0.001, 0.25)
        for name in ANALYSIS.EMPTY:
            phase = result["phases"][name]
            self.assertTrue(
                phase["all_averages_exceed_learned_baseline_even_at_minimum_gain"]
            )
            self.assertTrue(
                all(
                    x["certain_positive_reports"] == 1
                    for x in phase["sensitivity_counterfactuals"]
                )
            )
        self.assertEqual(
            result["supported_effective_threshold_range"], [0.001 / 30, 0.001]
        )

    def test_overlapping_labels_cannot_be_claimed_as_perfect_threshold_separation(self):
        phases = {name: [sample(0.01)] for name in ANALYSIS.PHASES}
        phases["empty_after"] = [sample(0.3)]
        phases["person_moving"] = [sample(0.02), sample(0.4)]
        separation = ANALYSIS.summarize(phases, 0.001, 0.25)[
            "single_threshold_separation"
        ]
        self.assertFalse(
            separation["perfect_separation_possible_for_recorded_averages"]
        )
        self.assertEqual(
            separation["occupied_reports_above_max_empty_by_phase"]["person_moving"],
            {"reports": 2, "certain_positive": 1},
        )

    def test_inconsistent_settings_or_presence_are_rejected(self):
        for baseline in (math.nan, 0.0, -1.0):
            with self.assertRaises(ValueError):
                ANALYSIS.summarize({}, baseline, 0.25)
        for sensitivity in (-0.1, 1.1, math.inf, math.nan):
            with self.assertRaises(ValueError):
                ANALYSIS.gain(sensitivity)
        for changed in (sample(threshold=0.5), sample(state="not_detected")):
            phases = {name: [changed] for name in ANALYSIS.PHASES}
            with self.assertRaises(ValueError):
                ANALYSIS.summarize(phases, 0.001, 0.25)


if __name__ == "__main__":
    unittest.main()
