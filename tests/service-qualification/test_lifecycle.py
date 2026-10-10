import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from lifecycle import ROOT, SCENARIOS, STARTUP_SCENARIOS, SUMMARY_FIELDS, expected_steps, run_case, sha256, validate


def transcript(scenario="normal", repetitions=1, *, foreign_owner=True):
    lines = []
    steps = expected_steps(scenario, repetitions, foreign_owner=foreign_owner)
    for status, services, events in steps:
        if status == 0 or scenario == "fail-apply" or (events and scenario in ("fail-stop", "fail-join", "fail-close")):
            lines.append(f"SQ_MEMORY before=0 full=0 delta=0 metadata=0 payload={services * 17 * 16} stacks={services * 4096}")
        if status == 0 or scenario == "fail-close":
            lines.append(f"SQ_RESULT services={services} queues={services * 3} events={events} received={events} errors={status} mean_cycles=240 max_cycles=480 misses_1ms=0 source=messages")
        if status == 1 and scenario in ("fail-stop", "fail-join"):
            lines.append("SQ_CLEANUP pending_threads=1 retained_queues=60")
        lines.append(f"SQ_DONE status={status} heap_after=0")
    failed = sum(status for status, _, _ in steps)
    row = dict(scenario=scenario, repetitions=repetitions, calls=len(steps), failures=failed,
               recoveries=sum(status == 0 for status, _, _ in steps) if scenario != "normal" else 0,
               fd_delta=0, thread_delta=0, queues_left=0)
    lines.append("SQ_LIFECYCLE " + " ".join(f"{key}={row[key]}" for key in SUMMARY_FIELDS))
    return "\n".join(lines)


class LifecycleEvidenceTests(unittest.TestCase):
    def test_published_host_matrix_is_complete_and_consistent(self):
        self.check_published_matrix("linux-lifecycle-2026-10-08.json", STARTUP_SCENARIOS,
                                    (220, 100, 100, 16000))

    def test_published_shutdown_matrix_is_complete_and_consistent(self):
        self.check_published_matrix("linux-shutdown-2026-10-08.json", SCENARIOS,
                                    (510, 290, 200, 26000))

    def check_published_matrix(self, filename, scenarios, expected_totals):
        path = Path(__file__).parent / "results" / filename
        evidence = json.loads(path.read_text())
        self.assertEqual(evidence["schema"], 1)
        self.assertTrue(evidence["host_only"])
        self.assertFalse(evidence["firmware_claim"])
        self.assertIsNone(evidence["failure"])
        self.assertEqual(evidence["repetitions"], 5)
        self.assertEqual(len(evidence["runs"]), len(scenarios) * 2)
        self.assertEqual({(row["language"], row["scenario"]) for row in evidence["runs"]},
                         {(language, scenario) for language in ("c", "rust") for scenario in scenarios})
        for row in evidence["runs"]:
            expected = validate(transcript(row["scenario"], 5), row["scenario"], 5)
            self.assertEqual(row, dict(language=row["language"], **expected))
        totals = tuple(sum(row[key] for row in evidence["runs"])
                       for key in ("calls", "failures", "recoveries", "delivered_events"))
        self.assertEqual(totals, expected_totals)
        for source, digest in evidence["source_sha256"].items():
            self.assertFalse(Path(source).is_absolute())
            self.assertNotIn("..", Path(source).parts)
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(set(evidence["binary_sha256"]), {"c", "rust"})
        for digest in evidence["binary_sha256"].values():
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
        for private_marker in ("/Users/", "/home/", "/tmp/", "SQ_RESULT", "SQ_MEMORY"):
            self.assertNotIn(private_marker, path.read_text())

    def test_all_scenarios_require_expected_failures_and_recovery(self):
        for scenario, calls, failures, delivered in (("normal", 2, 0, 600), ("fail-open", 2, 1, 100),
            ("fail-apply", 2, 1, 100), ("fail-queue", 8, 4, 400), ("fail-start", 8, 4, 400),
            ("fail-stop", 9, 6, 300), ("fail-join", 12, 9, 300), ("fail-close", 8, 4, 400)):
            with self.subTest(scenario=scenario):
                result = validate(transcript(scenario, 3), scenario, 3)
                self.assertEqual(result["calls"], calls * 3)
                self.assertEqual(result["failures"], failures * 3)
                self.assertEqual(result["delivered_events"], delivered * 3)

    def test_retained_shutdown_requires_complete_ownership_records(self):
        for scenario in ("fail-stop", "fail-join"):
            text = transcript(scenario)
            for bad in (text.replace("pending_threads=1", "pending_threads=0", 1),
                        text.replace("retained_queues=60", "retained_queues=0", 1),
                        text.replace("SQ_CLEANUP pending_threads=1 retained_queues=60\n", "", 1),
                        text.replace("SQ_CLEANUP", "SQ_CLEANUP extra=1", 1),
                        text.replace("payload=5440", "payload=0", 1)):
                with self.subTest(scenario=scenario), self.assertRaises(ValueError):
                    validate(bad, scenario, 1)

    def test_queue_close_error_requires_successful_business_delivery(self):
        text = transcript("fail-close")
        for bad in (text.replace("received=100", "received=99", 1),
                    text.replace("errors=1", "errors=0", 1),
                    text.replace("mean_cycles=240", "mean_cycles=999", 1),
                    text.replace("SQ_DONE status=1", "SQ_DONE status=0", 1)):
            with self.assertRaises(ValueError):
                validate(bad, "fail-close", 1)

    def test_missing_duplicate_or_truncated_summary_is_rejected(self):
        text = transcript()
        for bad in ("", text.rsplit("\n", 1)[0], text + "\n" + text.splitlines()[-1],
                    text + "\nSQ_DONE status=0 heap_after=0"):
            with self.subTest(text=bad), self.assertRaises(ValueError):
                validate(bad, "normal", 1)

    def test_leaked_resources_wrong_counts_and_invalid_numbers_are_rejected(self):
        text = transcript()
        for field in ("fd_delta", "thread_delta", "queues_left", "failures", "recoveries"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate(text.replace(field + "=0", field + "=1"), "normal", 1)
        for bad in (text.replace("calls=2", "calls=1"), text.replace("repetitions=1", "repetitions=2"),
                    text.replace("fd_delta=0", "fd_delta=-1"), text.replace("fd_delta=0", "fd_delta=nan")):
            with self.assertRaises(ValueError):
                validate(bad, "normal", 1)

    def test_each_success_and_failure_status_is_checked(self):
        text = transcript("fail-start")
        with self.assertRaises(ValueError):
            validate(text.replace("SQ_DONE status=1", "SQ_DONE status=0", 1), "fail-start", 1)
        with self.assertRaises(ValueError):
            validate(transcript().replace("received=300", "received=299", 1), "normal", 1)

    def test_unknown_output_and_unexpected_failure_records_are_rejected(self):
        text = transcript("fail-start")
        memory = transcript().splitlines()[0]
        result = transcript().splitlines()[1]
        for noise in ("unrecognized output", "SQ_OTHER value=1", "SQ_DONE_EXTRA status=1", ""):
            for bad in (noise + "\n" + text, text + "\n" + noise + "\n"):
                with self.subTest(noise=noise), self.assertRaises(ValueError):
                    validate(bad, "fail-start", 1)
        for extra in (memory, result):
            with self.assertRaises(ValueError):
                validate(extra + "\n" + text, "fail-start", 1)
        with self.assertRaises(ValueError):
            validate(None, "normal", 1)

    def test_apply_failure_requires_valid_full_capacity_record(self):
        text = transcript("fail-apply")
        first_memory = text.splitlines()[0]
        for bad in (text.replace(first_memory + "\n", "", 1),
                    text.replace("payload=5440", "payload=0", 1),
                    text.replace("delta=0", "delta=1", 1),
                    text.replace("SQ_MEMORY", "SQ_MEMORY extra=1", 1),
                    first_memory + "\n" + text):
            with self.assertRaises(ValueError):
                validate(bad, "fail-apply", 1)

    def test_apply_failure_accepts_only_expected_optional_result(self):
        text = transcript("fail-apply")
        result = "SQ_RESULT services=20 queues=60 events=100 received=0 errors=1 mean_cycles=0 max_cycles=0 misses_1ms=0 source=messages"
        valid = text.replace("SQ_DONE status=1", result + "\nSQ_DONE status=1", 1)
        self.assertEqual(validate(valid, "fail-apply", 1)["delivered_events"], 100)
        for field, value in (("received", "1"), ("errors", "0"), ("events", "99"),
                             ("source", "gpio"), ("max_cycles", "1")):
            bad_result = result.replace(field + "=" + ("messages" if field == "source" else
                                                        "100" if field == "events" else
                                                        "1" if field == "errors" else "0"),
                                        field + "=" + value)
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate(valid.replace(result, bad_result), "fail-apply", 1)

    def test_invalid_scenario_and_repeat_budget_are_rejected(self):
        for scenario, repetitions in (("unknown", 1), ("normal", 0), ("normal", 101),
                                      ("normal", True), ("normal", 1.5), ("normal", "1")):
            with self.assertRaises(ValueError):
                expected_steps(scenario, repetitions)

    def test_timeout_kills_only_the_owned_child_without_unlinking_pid_names(self):
        child = Mock(pid=12345, returncode=-9)
        child.communicate.side_effect = [subprocess.TimeoutExpired("fixture", 10), ("", "")]
        stale = Path("/dev/mqueue/sq_12345_0_0")
        with tempfile.TemporaryDirectory() as directory, \
                patch("lifecycle.subprocess.Popen", return_value=child), \
                patch.object(Path, "exists", autospec=True, side_effect=lambda path: path == stale), \
                patch.object(Path, "unlink", autospec=True) as unlink:
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                run_case(Path("c"), "normal", 1, Path(directory))
            child.kill.assert_called_once_with()
            unlink.assert_not_called()

    def test_failed_process_preserves_preexisting_pid_named_queues(self):
        child = Mock(pid=12345, returncode=1)
        child.communicate.return_value = ("", "initial owned queue count is 1, expected 0")
        with tempfile.TemporaryDirectory() as directory, \
                patch("lifecycle.subprocess.Popen", return_value=child), \
                patch.object(Path, "exists", return_value=True), \
                patch.object(Path, "unlink", autospec=True) as unlink:
            with self.assertRaisesRegex(RuntimeError, "exit 1"):
                run_case(Path("c"), "normal", 1, Path(directory))
            child.kill.assert_not_called()
            unlink.assert_not_called()

    def test_successful_process_with_stderr_does_not_qualify(self):
        child = Mock(pid=12345, returncode=0)
        child.communicate.return_value = (transcript(), "unrecognized diagnostic")
        with tempfile.TemporaryDirectory() as directory, \
                patch("lifecycle.subprocess.Popen", return_value=child):
            with self.assertRaisesRegex(ValueError, "unexpected stderr"):
                run_case(Path("c"), "normal", 1, Path(directory))

    def test_failed_process_and_invalid_success_output_do_not_qualify(self):
        with tempfile.TemporaryDirectory() as directory:
            child = Mock(pid=12345, returncode=1)
            child.communicate.return_value = ("", "fixture failure")
            with patch("lifecycle.subprocess.Popen", return_value=child), \
                    patch.object(Path, "exists", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "exit 1"):
                    run_case(Path("c"), "normal", 1, Path(directory))
            child.returncode = 0
            child.communicate.return_value = ("", "")
            with patch("lifecycle.subprocess.Popen", return_value=child):
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    run_case(Path("c"), "normal", 1, Path(directory))


@unittest.skipUnless(sys.platform == "linux" and os.environ.get("SQ_LIFECYCLE_DIR"),
                     "set SQ_LIFECYCLE_DIR to a fresh Linux lifecycle build")
class HostLifecycleIntegration(unittest.TestCase):
    def test_fresh_workers_restart_and_recover_without_owned_resource_leaks(self):
        folder = Path(os.environ["SQ_LIFECYCLE_DIR"]).resolve(strict=True)
        evidence = json.loads((folder / "report.json").read_text())
        self.assertIsNone(evidence["failure"])
        self.assertTrue(evidence["host_only"])
        self.assertFalse(evidence["firmware_claim"])
        for source, expected in evidence["source_sha256"].items():
            self.assertEqual(sha256(ROOT / source), expected, "stale lifecycle source: " + source)
        with tempfile.TemporaryDirectory() as directory:
            for language in ("c", "rust"):
                program = folder / language
                self.assertEqual(sha256(program), evidence["binary_sha256"][language])
                for scenario in SCENARIOS:
                    with self.subTest(language=language, scenario=scenario):
                        result = run_case(program, scenario, 1, Path(directory))
                        self.assertEqual((result["fd_delta"], result["thread_delta"], result["queues_left"]),
                                         (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
