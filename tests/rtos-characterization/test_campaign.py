"""Campaign-level omissions must not become an apparently complete comparison."""
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("characterization_campaign", Path(__file__).with_name("campaign.py"))
campaign = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(campaign)


class CampaignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plans = {observer: campaign.command_json(campaign.MPSC, [
            "plan", "--passes", 3, "--include-native-control", "--observer", observer])
            for observer in ("primary", "cpu", "irq")}

    def test_full_census_and_fresh_boot_boundaries(self):
        value = campaign.template(campaign.BOARDS)
        campaign.validate_manifest(value)
        rows = campaign.run_list(value, self.plans)
        counts = campaign.Counter(row["family"] for row in rows)
        self.assertEqual(counts, {"mpsc": 360, "owner": 14, "ordinary-ram": 78, "native-ram": 3})
        self.assertEqual(len({row["id"] for row in rows}), 455)
        self.assertEqual(sum(row.get("boots", 1) for row in rows), 464)
        self.assertTrue(all(row["runs_per_image"] == 1 for row in rows if row["family"] == "native-ram"))

    def test_complete_matched_passes_and_rotated_arms(self):
        rows = campaign.run_list(campaign.template(["esp32s3"]), self.plans)
        groups = campaign.defaultdict(list)
        for row in rows:
            if row["family"] == "mpsc":
                groups[row["observer"], row["case"], row["pass_number"]].append(row["runtime"] + "-" + row["backend"])
        for (observer, case, number), arms in groups.items():
            offset = number - 1
            expected = list(campaign.ARMS[offset:] + campaign.ARMS[:offset])
            self.assertEqual(arms, expected)
        self.assertEqual(len(groups), (16 + 4 + 4) * 3)

    def test_pico_does_not_invent_unavailable_observers_or_s3_ram(self):
        value = campaign.template(["pico2w"])
        campaign.validate_manifest(value)
        rows = campaign.run_list(value, self.plans)
        self.assertEqual(len(rows), 151)
        self.assertTrue(all(row.get("observer", "primary") == "primary" for row in rows))
        self.assertEqual(set(row["family"] for row in rows), {"mpsc", "owner"})

    def test_no_missing_native_control_or_relabelled_host_build(self):
        value = campaign.template(campaign.BOARDS)
        for key in ("esp32s3/irq/nuttx-native", "pico2w/owner", "ordinary-ram/ram-crossbeam-select"):
            changed = copy.deepcopy(value)
            changed["builds"].pop(key)
            with self.assertRaisesRegex(ValueError, "campaign build"):
                campaign.validate_manifest(changed)
        value["builds"]["host/owner"] = "target/host-owner"
        with self.assertRaisesRegex(ValueError, "campaign build"):
            campaign.validate_manifest(value)

    def test_one_pass_and_boolean_pass_count_cannot_claim_measured_campaign(self):
        for passes in (1, 2, True, "3"):
            value = campaign.template(["esp32s3"])
            value["passes"] = passes
            with self.assertRaisesRegex(ValueError, "three separate passes"):
                campaign.validate_manifest(value)

    def test_duplicate_board_and_json_fields_fail(self):
        with self.assertRaises(ValueError):
            campaign.validate_manifest(campaign.template(["esp32s3", "esp32s3"]))
        with self.assertRaisesRegex(ValueError, "duplicate JSON"):
            campaign.unique([("run", 1), ("run", 2)])

    def test_missing_duplicate_extra_and_reused_capture_fail(self):
        ready = {"campaign_id": "fixture", "runs": [{"id": "a"}, {"id": "b"}]}
        ledger = {"schema": "rtos-characterization-ledger-v1", "campaign_id": "fixture",
                  "runs": [{"id": "a", "evidence": "target/a"}, {"id": "b", "evidence": "target/b"}]}
        self.assertEqual(set(campaign.evidence_census(ready, ledger)), {"a", "b"})
        for rows in (ledger["runs"][:1], ledger["runs"] + ledger["runs"][:1],
                     ledger["runs"] + [{"id": "c", "evidence": "target/c"}],
                     [{"id": "a", "evidence": "target/a"}, {"id": "b", "evidence": "target/a"}]):
            changed = dict(ledger, runs=rows)
            with self.assertRaises(ValueError):
                campaign.evidence_census(ready, changed)

    def test_changed_plan_fails_before_any_validator_or_device_access(self):
        value = {"schema": "rtos-characterization-ready-v1", "inputs": {}, "runs": []}
        value["campaign_id"] = campaign.identity(value)
        value["runs"].append({"id": "omitted"})
        with self.assertRaisesRegex(ValueError, "changed readiness"):
            campaign.validate_ready(value)

    def test_old_valid_capture_needs_this_campaign_and_run_session(self):
        ready, run = {"campaign_id": "new"}, {"id": "expected"}
        campaign.session_scope({"campaign_id": "new", "run_id": "expected"}, ready, run)
        for session in ({}, {"campaign_id": "old", "run_id": "expected"},
                        {"campaign_id": "new", "run_id": "different"}):
            with self.assertRaisesRegex(ValueError, "different campaign or run"):
                campaign.session_scope(session, ready, run)

    def test_preserve_existing_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            campaign.write_new(path, {"original": True})
            with self.assertRaises(FileExistsError):
                campaign.write_new(path, {"original": False})
            self.assertEqual(campaign.read(path), {"original": True})


if __name__ == "__main__":
    unittest.main()
