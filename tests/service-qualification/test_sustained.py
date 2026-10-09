"""Faster-paced delivery is a separate cohort, not proof of overload."""
import copy
import json
import unittest

from publish import public_report, sustained_report
from test_publish import report as footprint_fixture


def fixture():
    report = footprint_fixture()
    prototype = report["runs"][0]
    report.update(period_us=100, events=20000, recovery_events=100, blocks=2,
                  services=[20], harness_sha256={"tests/service-qualification/measure.py": "hash"}, runs=[])
    for block in range(2):
        for language in (("c", "rust") if block == 0 else ("rust", "c")):
            row = copy.deepcopy(prototype)
            row.update(block=block, language=language, command_wall_seconds=20.5)
            row["result"].update(services=20, queues=60, events=20000, received=20000)
            row["memory"].update(stacks=81920, payload=5440)
            row["nsh_after"]["nused"] = 32
            recovery = copy.deepcopy(row)
            recovery["result"].update(events=100, received=100)
            recovery.update(period_us=2000, command_wall_seconds=.5,
                            nsh_before=copy.deepcopy(row["nsh_after"]))
            row["recovery"] = recovery
            report["runs"].append(row)
    return report


class SustainedPublicationTests(unittest.TestCase):
    def test_explicit_rate_opt_in_keeps_existing_cohorts_separate(self):
        report = fixture()
        with self.assertRaises(ValueError):
            public_report([report])
        self.assertEqual(public_report([report], period_us=100)["period_us"], 100)
        with self.assertRaises(ValueError):
            public_report([report, footprint_fixture()], period_us=100)

    def test_complete_private_free_delivery_and_recovery_evidence(self):
        public = sustained_report(fixture())
        self.assertEqual(len(public["runs"]), 4)
        self.assertFalse(public["overload_qualified"])
        self.assertFalse(public["retry_counts_available"])
        self.assertTrue(public["same_boot_recovery"])
        self.assertFalse(public["same_process"])
        for row in public["runs"]:
            self.assertEqual(row["events"], 20000)
            self.assertEqual(row["recovery"]["received"], 100)
            self.assertEqual(row["recovery"]["post_load_heap_change_bytes"], 0)
        for private in ("/private", "transcript", "backup_sha256", "rustflags"):
            self.assertNotIn(private, json.dumps(public))

    def test_growth_is_reported_not_hidden(self):
        report = fixture()
        report["runs"][0]["recovery"]["nsh_after"].update(used=116, nused=33)
        recovery = sustained_report(report)["runs"][0]["recovery"]
        self.assertEqual((recovery["post_load_heap_change_bytes"],
                          recovery["post_load_allocation_change"]), (16, 1))

    def test_incomplete_reordered_failed_or_disconnected_recovery_is_rejected(self):
        for mutate in (lambda r: r["runs"].pop(), lambda r: r["runs"].reverse(),
                       lambda r: r["runs"].append(copy.deepcopy(r["runs"][0])),
                       lambda r: r.update(events=100), lambda r: r.update(recovery_events=0),
                       lambda r: r["runs"][0].pop("recovery"),
                       lambda r: r["runs"][0]["recovery"].update(period_us=100),
                       lambda r: r["runs"][0]["result"].update(events=19999),
                       lambda r: r["runs"][0]["recovery"]["result"].update(received=99),
                       lambda r: r["runs"][0]["recovery"]["done"].update(status=1),
                       lambda r: r["runs"][0]["recovery"]["memory"].update(stacks=0),
                       lambda r: r["runs"][0]["recovery"]["nsh_before"].update(used=1),
                       lambda r: r.update(restoration_verified=False),
                       lambda r: r.update(harness_sha256={"/private/fixture.py": "hash"})):
            report = fixture()
            mutate(report)
            with self.assertRaises(ValueError):
                sustained_report(report)

    def test_nonfinite_or_missing_wall_times_are_rejected(self):
        for value in (None, True, 0, -1, float("nan"), float("inf"), "20"):
            for recovery in (False, True):
                report = fixture()
                row = report["runs"][0]
                if recovery:
                    row = row["recovery"]
                row["command_wall_seconds"] = value
                with self.subTest(value=value, recovery=recovery), self.assertRaises(ValueError):
                    sustained_report(report)


if __name__ == "__main__":
    unittest.main()
