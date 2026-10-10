import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from test_lifecycle import transcript


HERE = Path(__file__).resolve().parent


def load_device_faults():
    # Some legacy imports add their own directory to sys.path. Keep that
    # historical side effect local to this import and this test process.
    with patch.object(sys, "path", sys.path.copy()):
        sys.path.insert(0, str(HERE))
        spec = importlib.util.spec_from_file_location("sq_device_faults_test", HERE / "device_faults.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


device_faults = load_device_faults()


def runtime_segments(scenario, repetitions=1):
    lines = transcript(scenario, repetitions, foreign_owner=False).splitlines()
    segments, current = [], []
    for line in lines:
        if line.startswith("SQ_LIFECYCLE"):
            break
        current.append(line)
        if line.startswith("SQ_DONE "):
            segments.append(current)
            current = []
    return segments


def valid_device_transcript():
    lines = ["SQ_DEVICE_WARMUP", *runtime_segments("fail-close")[1],
             "SQ_DEVICE_BASELINE heap=42"]
    totals = {"fail-stop": (27, 18, 9), "fail-join": (27, 18, 9), "fail-close": (24, 12, 12)}
    for scenario in device_faults.SCENARIOS:
        calls, failures, recoveries = totals[scenario]
        lines.append(f"SQ_DEVICE_BEGIN scenario={scenario}")
        segments = runtime_segments(scenario, device_faults.REPETITIONS)
        positions = (0, 1, 30, 59) if scenario == "fail-close" else (0, 7, 19)
        stages = ("failed", "recovery") if scenario == "fail-close" else ("failed", "retry", "recovery")
        segment_index = 0
        for _ in range(device_faults.REPETITIONS):
            for position in positions:
                for stage in stages:
                    lines.append(f"SQ_DEVICE_CASE position={position} stage={stage}")
                    lines.extend(segments[segment_index])
                    segment_index += 1
                    retained = stage != "recovery" and scenario != "fail-close"
                    hits = 1 if stage == "failed" or scenario == "fail-close" else 2
                    lines.append("SQ_OWNED descriptors={} handles={} names={} heap=42 fault_hits={} elapsed_ms=12".format(
                        60 if retained else 0, 1 if retained else 0, 60 if retained else 0, hits))
        lines.append(
            f"SQ_DEVICE_FAULT scenario={scenario} repetitions=3 calls={calls} failures={failures} "
            f"recoveries={recoveries} descriptors=0 handles=0 names=0 heap_growth=0")
    lines.append("SQ_DEVICE_BATCH repetitions=3 calls=79 failures=48 recoveries=30 heap_before=42 heap_after=42 status=0")
    return "\n".join(lines)


def mutate_line(text, prefix, old, new, occurrence=0):
    rows = text.splitlines()
    matches = [index for index, row in enumerate(rows) if row.startswith(prefix)]
    rows[matches[occurrence]] = rows[matches[occurrence]].replace(old, new, 1)
    return "\n".join(rows)


class DeviceFaultProtocolTests(unittest.TestCase):
    def test_published_matrix_is_complete_private_and_matches_shared_runtime(self):
        path = HERE / "results/esp32s3-shutdown-faults-2026-10-08.json"
        evidence = json.loads(path.read_text())
        self.assertEqual(evidence["schema"], 1)
        self.assertEqual(tuple(evidence[key] for key in
                              ("calls", "expected_failures", "recoveries", "verified_events")),
                         (316, 192, 120, 12400))
        for flag in ("diagnostic_fault_injection", "same_coordinator", "restoration_verified"):
            self.assertTrue(evidence[flag])
        for flag in ("performance_claim", "foreign_task_recovery_qualified", "interrupt_qualified"):
            self.assertFalse(evidence[flag])
        rows = evidence["summaries"]
        self.assertEqual([(row["block"], row["language"]) for row in rows],
                         [(0, "c"), (0, "rust"), (1, "rust"), (1, "c")])
        expected = device_faults.parse_faults(valid_device_transcript())
        for row in rows:
            baseline = 20324 if row["language"] == "c" else 20468
            self.assertEqual((row["heap_before"], row["heap_after"]), (baseline, baseline))
            for key in ("repetitions", "calls", "failures", "recoveries", "status",
                        "verified_warmup_events", "verified_recovery_events"):
                self.assertEqual(row[key], expected[key])
            for profile, prototype in zip(row["profiles"], expected["profiles"]):
                self.assertLess(profile["max_call_ms"], 2000)
                self.assertGreaterEqual(profile["max_call_ms"], 0)
                self.assertEqual({key: value for key, value in profile.items() if key != "max_call_ms"},
                                 {key: value for key, value in prototype.items() if key != "max_call_ms"})
            self.assertEqual(len(row["profiles"]), 3)
        host = json.loads((HERE / "results/linux-shutdown-2026-10-08.json").read_text())
        for language, build in evidence["builds"].items():
            for source in ("runtime.c", "lifecycle_faults.c", "lifecycle_faults.h"):
                name = "tests/service-qualification/" + source
                self.assertEqual(build["source_sha256"][name], host["source_sha256"][name])
            for inventory in (build["source_sha256"], evidence["harness_sha256"]):
                for source, digest in inventory.items():
                    self.assertFalse(Path(source).is_absolute())
                    self.assertNotIn("..", Path(source).parts)
                    self.assertRegex(digest, r"^[0-9a-f]{64}$")
            for digest in build["artifacts"].values():
                self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(evidence["builds"]["c"]["config_identity"],
                         evidence["builds"]["rust"]["config_identity"])
        self.assertEqual(evidence["builds"]["c"]["kernel_inventory_sha256"],
                         evidence["builds"]["rust"]["kernel_inventory_sha256"])
        for marker in ("/Users/", "/home/", "/tmp/", "transcript", "backup_sha256", "SQ_RESULT"):
            self.assertNotIn(marker, path.read_text())

    def test_public_export_requires_complete_recovered_and_restored_matrix(self):
        evidence = json.loads((HERE / "results/esp32s3-shutdown-faults-2026-10-08.json").read_text())
        report = dict(blocks=2, failure=None, restoration_error=None, restoration_verified=True,
                      fault_injection=True, runs=copy.deepcopy(evidence["summaries"]),
                      builds=copy.deepcopy(evidence["builds"]), harness_sha256=evidence["harness_sha256"])
        for build in report["builds"].values():
            build.update(diagnostic_faults=True, kernel_archives={}, compiler_input=None,
                         kernel_header_sha256="a" * 64)
        report["runs"][0]["private_transcript"] = "/private/diagnostic.txt"
        report["runs"][0]["profiles"][0]["private_detail"] = "omit me"
        public = device_faults.public_report(report)
        self.assertTrue(public["paired_kernel_headers_verified"])
        for build_row in public["builds"].values():
            self.assertEqual(build_row["kernel_header_sha256"], "a" * 64)
        self.assertEqual(public["calls"], 316)
        self.assertNotIn("private_", json.dumps(public))
        mutations = (
            lambda item: item.update(failure="capture failed"),
            lambda item: item.update(restoration_error="verification failed"),
            lambda item: item.update(restoration_verified=False),
            lambda item: item.update(fault_injection=False),
            lambda item: item["runs"].pop(),
            lambda item: item["runs"].reverse(),
            lambda item: item["runs"][0].update(heap_after=20325),
            lambda item: item["runs"][0]["profiles"][0].update(handles=1),
            lambda item: item["builds"]["c"].update(diagnostic_faults=False),
            lambda item: item["builds"]["rust"].update(kernel_header_sha256="b" * 64),
            lambda item: item["builds"]["c"].update(kernel_header_sha256="invalid"),
        )
        for mutate in mutations:
            item = copy.deepcopy(report)
            mutate(item)
            with self.subTest(mutation=mutations.index(mutate)), self.assertRaises(ValueError):
                device_faults.public_report(item)

    def test_valid_target_batch_verifies_expected_calls_and_events(self):
        result = device_faults.parse_faults(valid_device_transcript())
        self.assertEqual((result["calls"], result["failures"], result["recoveries"]), (79, 48, 30))
        self.assertEqual(result["verified_warmup_events"], 100)
        self.assertEqual(result["verified_recovery_events"], 3000)
        self.assertEqual([row["scenario"] for row in result["profiles"]], list(device_faults.SCENARIOS))
        self.assertEqual([row["verified_recovery_events"] for row in result["profiles"]], [900, 900, 1200])

    def test_missing_duplicate_and_reordered_protocol_sections_are_rejected(self):
        text = valid_device_transcript()
        first_case = next(row for row in text.splitlines() if row.startswith("SQ_DEVICE_CASE"))
        first_owned = next(row for row in text.splitlines() if row.startswith("SQ_OWNED"))
        first_fault = next(row for row in text.splitlines() if row.startswith("SQ_DEVICE_FAULT"))
        batch = next(row for row in text.splitlines() if row.startswith("SQ_DEVICE_BATCH"))
        malformed = (
            text.replace("SQ_DEVICE_WARMUP\n", "", 1),
            text + "\n" + batch,
            text.replace(first_case + "\n", "", 1),
            text.replace(first_owned + "\n", "", 1),
            text.replace(first_fault + "\n", "", 1),
            text.replace("SQ_DEVICE_BEGIN scenario=fail-stop", "SQ_DEVICE_BEGIN scenario=fail-join", 1),
            text.replace("SQ_DEVICE_BATCH repetitions=3", "SQ_DEVICE_FAULT repetitions=3", 1),
        )
        for bad in malformed:
            with self.subTest(sample=bad[:90]), self.assertRaises(ValueError):
                device_faults.parse_faults(bad)

    def test_resource_fault_hit_heap_and_elapsed_invariants_are_enforced(self):
        text = valid_device_transcript()
        owned = next(row for row in text.splitlines() if row.startswith("SQ_OWNED"))
        bad_owned = []
        for old, new in (("descriptors=60", "descriptors=59"), ("handles=1", "handles=0"),
                         ("names=60", "names=0"), ("fault_hits=1", "fault_hits=0"),
                         ("elapsed_ms=12", "elapsed_ms=2000"), ("heap=42", "heap=41")):
            bad_owned.append(text.replace(owned, owned.replace(old, new, 1), 1))
        # Recovery must return precisely to the post-warmup baseline.
        rows = text.splitlines()
        recovery_index = next(i for i, row in enumerate(rows)
                              if row == "SQ_DEVICE_CASE position=0 stage=recovery")
        owned_index = next(i for i in range(recovery_index + 1, len(rows))
                           if rows[i].startswith("SQ_OWNED"))
        rows[owned_index] = rows[owned_index].replace("heap=42", "heap=43", 1)
        bad_owned.append("\n".join(rows))
        for bad in bad_owned:
            with self.subTest(owned=bad_owned.index(bad)), self.assertRaises(ValueError):
                device_faults.parse_faults(bad)

    def test_runtime_business_delivery_and_batch_counts_are_enforced(self):
        text = valid_device_transcript()
        result = next(row for row in text.splitlines() if row.startswith("SQ_RESULT"))
        batch = next(row for row in text.splitlines() if row.startswith("SQ_DEVICE_BATCH"))
        summary = next(row for row in text.splitlines() if row.startswith("SQ_DEVICE_FAULT"))
        cases = next(row for row in text.splitlines() if row.startswith("SQ_DEVICE_CASE"))
        bad = (
            text.replace(result, result.replace("received=100", "received=99", 1), 1),
            text.replace(result, result.replace("events=100", "events=99", 1), 1),
            text.replace(cases, "SQ_DEVICE_CASE position=7 stage=failed", 1),
            text.replace(batch, batch.replace("calls=79", "calls=78", 1), 1),
            text.replace(batch, batch.replace("failures=48", "failures=47", 1), 1),
            text.replace(batch, batch.replace("recoveries=30", "recoveries=29", 1), 1),
            text.replace(batch, batch.replace("heap_after=42", "heap_after=43", 1), 1),
            text.replace(summary, summary.replace("calls=27", "calls=26", 1), 1),
        )
        for candidate in bad:
            with self.subTest(sample=candidate[:100]), self.assertRaises(ValueError):
                device_faults.parse_faults(candidate)


if __name__ == "__main__":
    unittest.main()
