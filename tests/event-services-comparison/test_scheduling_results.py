"""Consistency checks for the dated, sanitized hardware evidence."""
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import unittest

HERE = Path(__file__).resolve().parent


class SchedulingEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (HERE / "results/esp32s3-scheduling-2026-10-04.json").read_text()
        cls.report = json.loads(cls.text)

    def test_complete_matched_matrix_and_delivery_accounting(self):
        report = self.report
        self.assertEqual(report["kind"], "event-services-scheduling-report")
        self.assertEqual((report["blocks"], report["runs_per_profile"]), (2, 2))
        self.assertEqual(set(report["cases"]), {
            "nuttx-c-three", "nuttx-rust-three", "zephyr-c-three",
            "embassy-three-event", "embassy-three-natural",
            "embassy-three-budget", "embassy-three-chunked",
        })
        source_maps, count, attempts = [], 0, 0
        for case in report["cases"].values():
            source_maps.append(case["source_sha256"])
            self.assertEqual(case["blocks"], [0, 1])
            self.assertEqual(set(case["profiles"]), {
                "normal", "burst", "work-short", "work-medium", "work-long", "io-wait",
            })
            for profile, observations in case["profiles"].items():
                self.assertEqual(len(observations["runs"]), 4)
                for row in observations["runs"]:
                    self.assertEqual(row["attempted"], 6240 if profile == "burst" else 3900)
                    self.assertEqual(row["accepted"], row["received"])
                    self.assertEqual(row["accepted"] + row["rejected"], row["attempted"])
                    self.assertEqual(row["protocol_errors"], 0)
                    if profile != "work-long":
                        self.assertEqual((row["rejected"], row["missed"]), (0, 0))
                    count += 1
                    attempts += row["attempted"]
        self.assertTrue(all(source == source_maps[0] for source in source_maps))
        self.assertEqual((count, attempts), (168, 720720))

    def test_natural_io_and_light_work_qualify_without_explicit_handoffs(self):
        natural = self.report["cases"]["embassy-three-natural"]
        for profile, observations in natural["profiles"].items():
            for row in observations["runs"]:
                self.assertEqual(row["yields"], 0)
                if profile != "work-long":
                    self.assertEqual((row["rejected"], row["missed"]), (0, 0))
                if profile == "io-wait":
                    self.assertEqual(row["io_jobs"], 117)
                    self.assertGreaterEqual(row["io_p99_us"], 3000)
        for case in self.report["cases"].values():
            for row in case["profiles"]["io-wait"]["runs"]:
                self.assertEqual((row["rejected"], row["missed"], row["io_jobs"]), (0, 0, 117))

    def test_public_evidence_has_no_raw_transcripts_or_private_paths(self):
        for name in ("esp32s3-scheduling-2026-10-04.json",
                     "esp32s3-compiler-baseline-2026-10-05.json",
                     "esp32s3-compiler-patched-2026-10-05.json"):
            text = (HERE / "results" / name).read_text()
            self.assertNotIn('"raw_output"', text)
            for private in ("/Users/", "/home/", "usbmodem", "device-before.bin"):
                self.assertNotIn(private, text)



class CompilerEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before_path = HERE / "results/esp32s3-compiler-baseline-2026-10-05.json"
        cls.before = json.loads(cls.before_path.read_text())
        cls.after = json.loads((HERE / "results/esp32s3-compiler-patched-2026-10-05.json").read_text())

    def test_unchanged_portable_inputs_and_complete_loss_free_matrix(self):
        report = self.after
        self.assertEqual((report["blocks"], report["runs_per_profile"]), (2, 2))
        self.assertEqual(set(report["cases"]), {
            "nuttx-c-three", "nuttx-rust-three", "zephyr-c-three",
            "embassy-three-natural", "embassy-three-chunked",
        })
        source = self.before["cases"]["nuttx-rust-three"]["source_sha256"]
        self.assertNotIn("work_xtensa.rs", source)
        for name, expected in source.items():
            path = HERE / name
            self.assertTrue(path.is_file())
            self.assertRegex(expected, r"^[0-9a-f]{64}$")
            # Tool metadata can change without changing measured firmware.
            # Preserve its recorded identity; still freeze all firmware inputs.
            if path.suffix != ".py":
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)
        invocations, attempts = 0, 0
        for case in report["cases"].values():
            self.assertEqual(case["source_sha256"], source)
            self.assertEqual(set(case["profiles"]), {
                "normal", "work-medium", "work-long", "io-wait",
            })
            for profile, observations in case["profiles"].items():
                rows = observations["runs"]
                self.assertEqual(len(rows), 4)
                for row in rows:
                    self.assertEqual((row["attempted"], row["accepted"], row["received"]),
                                     (3900, 3900, 3900))
                    self.assertEqual((row["rejected"], row["protocol_errors"]), (0, 0))
                    self.assertEqual(row["work_jobs"], 117 if profile.startswith("work-") else 0)
                    self.assertEqual(row["io_jobs"], 117 if profile == "io-wait" else 0)
                    if profile != "work-long":
                        self.assertEqual(row["missed"], 0)
                    for key, summary in observations["metrics"].items():
                        values = [run[key] for run in rows]
                        self.assertEqual(summary, {"median": statistics.median(values), "max": max(values)})
                    invocations += 1
                    attempts += row["attempted"]
        self.assertEqual((invocations, attempts), (80, 312000))

    def test_compiler_evidence_binds_the_actual_ordered_patchset(self):
        root = HERE.parents[1]
        pins = json.loads((root / "upstream/rust-llvm/upstream.json").read_text())
        evidence = self.after["compiler_evaluation"]
        ledger = evidence["patch_ledger"]
        self.assertTrue(evidence["restore_verified"])
        self.assertTrue(self.before["compiler_evaluation"]["restore_verified"])
        self.assertEqual(ledger["upstream_revision"], pins["revision"])
        self.assertEqual(ledger["rust_revision"], pins["rust_revision"])
        self.assertEqual(ledger["qualification"], "draft-unqualified")
        self.assertEqual(evidence["baseline_record_sha256"],
                         hashlib.sha256(self.before_path.read_bytes()).hexdigest())
        states = dict(pins["before_sha256"])
        self.assertEqual(len(ledger["patches"]), 5)
        for entry in ledger["patches"]:
            path = root / "upstream/rust-llvm/patches" / entry["name"]
            self.assertEqual(entry["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            for name, hashes in entry["files"].items():
                self.assertEqual(hashes["before"], states.get(name))
                self.assertRegex(hashes["after"], r"^[0-9a-f]{64}$")
                states[name] = hashes["after"]
        self.assertEqual(evidence["backend_tests"],
                         {"codegen_passed": 62, "mc_passed": 36, "total_passed": 98})
        compiler = evidence["compiler"]
        self.assertIn("commit-hash: unknown", compiler["compiler_version"])
        self.assertRegex(compiler["compiler_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(compiler["compiler_libraries_sha256"])
        inputs = evidence["rust_image_inputs"]
        self.assertEqual(set(inputs), {"nuttx-rust-three", "embassy-three-natural", "embassy-three-chunked"})
        self.assertEqual(len({value["std_source_inventory_sha256"] for value in inputs.values()}), 1)
        for value in inputs.values():
            self.assertEqual(value["app_opt_level"], "2")
            self.assertRegex(value["build_provenance_sha256"], r"^[0-9a-f]{64}$")

    def test_size_deltas_and_deadline_failures_are_not_hidden(self):
        c = self.after["cases"]["nuttx-c-three"]
        rust = self.after["cases"]["nuttx-rust-three"]
        original = self.before["cases"]["nuttx-rust-three"]
        self.assertEqual(rust["loaded_flash_bytes"] - c["loaded_flash_bytes"], 840)
        self.assertEqual(rust["image_bytes"] - c["image_bytes"], 24)
        self.assertEqual(rust["resident_ram_bytes"] - c["resident_ram_bytes"], 8)
        self.assertEqual(rust["loaded_flash_bytes"] - original["loaded_flash_bytes"], 16)
        self.assertEqual(rust["image_bytes"], original["image_bytes"])
        self.assertEqual(rust["resident_ram_bytes"], original["resident_ram_bytes"])
        expected_misses = {"nuttx-c-three": 3, "nuttx-rust-three": 60, "zephyr-c-three": 0,
                           "embassy-three-natural": 41, "embassy-three-chunked": 28}
        for name, missed in expected_misses.items():
            rows = self.after["cases"][name]["profiles"]["work-long"]["runs"]
            self.assertEqual(sum(row["missed"] for row in rows), missed)
        self.assertGreater(rust["profiles"]["work-long"]["metrics"]["work_max_us"]["max"],
                           c["profiles"]["work-long"]["metrics"]["work_max_us"]["max"])


class ScheduledCompilerEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (HERE / "results/esp32s3-compiler-scheduled-2026-10-05.json").read_text()
        cls.report = json.loads(cls.text)
        cls.before_path = HERE / "results/esp32s3-compiler-patched-2026-10-05.json"
        cls.before = json.loads(cls.before_path.read_text())

    def test_unchanged_full_matrix_retains_delivery_and_deadline_results(self):
        report = self.report
        self.assertEqual((report["blocks"], report["runs_per_profile"]), (2, 2))
        self.assertEqual(set(report["cases"]), set(self.before["cases"]))
        misses = {"nuttx-c-three": 11, "nuttx-rust-three": 50, "zephyr-c-three": 0,
                  "embassy-three-natural": 16, "embassy-three-chunked": 28}
        invocations = attempts = 0
        for name, case in report["cases"].items():
            self.assertEqual(case["source_sha256"], self.before["cases"][name]["source_sha256"])
            for profile, observed in case["profiles"].items():
                self.assertEqual(len(observed["runs"]), 4)
                for row in observed["runs"]:
                    self.assertEqual((row["attempted"], row["accepted"], row["received"]), (3900, 3900, 3900))
                    self.assertEqual((row["rejected"], row["protocol_errors"]), (0, 0))
                    self.assertEqual(row["work_jobs"], 117 if profile.startswith("work-") else 0)
                    self.assertEqual(row["io_jobs"], 117 if profile == "io-wait" else 0)
                    if profile != "work-long": self.assertEqual(row["missed"], 0)
                    invocations += 1
                    attempts += row["attempted"]
                for key, summary in observed["metrics"].items():
                    values = [row[key] for row in observed["runs"]]
                    self.assertEqual(summary, {"median": statistics.median(values), "max": max(values)})
            self.assertEqual(sum(r["missed"] for r in case["profiles"]["work-long"]["runs"]), misses[name])
        self.assertEqual((invocations, attempts), (80, 312000))
        for name in ("nuttx-c-three", "zephyr-c-three"):
            self.assertEqual(report["cases"][name]["image_sha256"], self.before["cases"][name]["image_sha256"])
        rust, c = report["cases"]["nuttx-rust-three"], report["cases"]["nuttx-c-three"]
        for key, delta in (("loaded_flash_bytes", 840), ("image_bytes", 24), ("resident_ram_bytes", 8)):
            self.assertEqual(rust[key], self.before["cases"]["nuttx-rust-three"][key])
            self.assertEqual(rust[key] - c[key], delta)
        self.assertEqual(rust["profiles"]["work-long"]["metrics"]["work_max_us"]["max"], 13759)

    def test_report_binds_current_compiler_patchset_and_keeps_private_data_out(self):
        root = HERE.parents[1]
        pins = json.loads((root / "upstream/rust-llvm/upstream.json").read_text())
        evidence = self.report["compiler_evaluation"]
        self.assertTrue(evidence["restore_verified"])
        self.assertEqual(evidence["baseline_record_sha256"], hashlib.sha256(self.before_path.read_bytes()).hexdigest())
        ledger = evidence["patch_ledger"]
        self.assertEqual(ledger["upstream_revision"], pins["revision"])
        self.assertEqual(ledger["rust_revision"], pins["rust_revision"])
        self.assertEqual(len(ledger["patches"]), 6)
        states = dict(pins["before_sha256"])
        for entry in ledger["patches"]:
            path = root / "upstream/rust-llvm/patches" / entry["name"]
            self.assertEqual(entry["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            for name, hashes in entry["files"].items():
                self.assertEqual(hashes["before"], states.get(name))
                states[name] = hashes["after"]
        self.assertEqual(evidence["backend_tests"], {"codegen_passed": 63, "mc_passed": 36, "total_passed": 99})
        self.assertNotEqual(evidence["compiler"]["compiler_libraries_sha256"],
                            self.before["compiler_evaluation"]["compiler"]["compiler_libraries_sha256"])
        self.assertEqual(set(evidence["rust_image_inputs"]),
                         {"nuttx-rust-three", "embassy-three-natural", "embassy-three-chunked"})
        for private in ('"raw_output"', "/Users/", "/home/", "usbmodem", "device-before.bin"):
            self.assertNotIn(private, self.text)


class AlignedCompilerEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (HERE / "results/esp32s3-compiler-aligned-2026-10-05.json").read_text()
        cls.report = json.loads(cls.text)
        cls.before_path = HERE / "results/esp32s3-compiler-scheduled-2026-10-05.json"
        cls.before = json.loads(cls.before_path.read_text())

    def test_native_driver_matrix_preserves_inputs_and_every_delivery(self):
        report = self.report
        self.assertEqual((report["blocks"], report["runs_per_profile"]), (2, 2))
        self.assertEqual(set(report["cases"]), {"nuttx-c-three", "nuttx-rust-three"})
        profiles = {"normal", "work-medium", "work-long", "io-wait"}
        source = self.before["cases"]["nuttx-rust-three"]["source_sha256"]
        self.assertNotIn("work_xtensa.rs", source)
        for name, expected in source.items():
            path = HERE / name
            self.assertTrue(path.is_file())
            self.assertRegex(expected, r"^[0-9a-f]{64}$")
            # Tool metadata can change without changing measured firmware.
            # Preserve its recorded identity; still freeze all firmware inputs.
            if path.suffix != ".py":
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)
        invocations = deliveries = 0
        for case in report["cases"].values():
            self.assertEqual(case["source_sha256"], source)
            self.assertEqual(case["blocks"], [0, 1])
            self.assertEqual(set(case["profiles"]), profiles)
            for profile, observed in case["profiles"].items():
                rows = observed["runs"]
                self.assertEqual(len(rows), 4)
                for row in rows:
                    self.assertEqual((row["attempted"], row["accepted"], row["received"]),
                                     (3900, 3900, 3900))
                    self.assertEqual((row["rejected"], row["protocol_errors"]), (0, 0))
                    self.assertEqual(row["work_jobs"], 117 if profile.startswith("work-") else 0)
                    self.assertEqual(row["io_jobs"], 117 if profile == "io-wait" else 0)
                    if profile != "work-long":
                        self.assertEqual(row["missed"], 0)
                    invocations += 1
                    deliveries += row["received"]
                for key, summary in observed["metrics"].items():
                    values = [row[key] for row in rows]
                    self.assertEqual(summary, {"median": statistics.median(values), "max": max(values)})
        self.assertEqual((invocations, deliveries), (32, 124800))
        c, rust = (report["cases"][name] for name in ("nuttx-c-three", "nuttx-rust-three"))
        self.assertEqual(c["configuration"], rust["configuration"])
        self.assertEqual(c["image_sha256"], self.before["cases"]["nuttx-c-three"]["image_sha256"])

    def test_footprint_and_remaining_deadline_failures_are_retained(self):
        c, rust = (self.report["cases"][name] for name in ("nuttx-c-three", "nuttx-rust-three"))
        for key, delta in (("loaded_flash_bytes", 844), ("image_bytes", 24), ("resident_ram_bytes", 8)):
            self.assertEqual(rust[key] - c[key], delta)
        self.assertEqual(rust["loaded_flash_bytes"] - self.before["cases"]["nuttx-rust-three"]["loaded_flash_bytes"], 4)
        for name, misses, longest in (("nuttx-c-three", 1, 11311), ("nuttx-rust-three", 16, 11762)):
            case = self.report["cases"][name]
            long_work = case["profiles"]["work-long"]
            self.assertEqual(sum(row["missed"] for row in long_work["runs"]), misses)
            self.assertEqual(long_work["metrics"]["work_max_us"]["max"], longest)
        self.assertEqual((c["peak_heap_bytes"], rust["peak_heap_bytes"]), (117552, 118104))

    def test_report_binds_the_inactive_proposal_and_actual_rust_driver(self):
        root = HERE.parents[1]
        evidence = self.report["compiler_evaluation"]
        self.assertTrue(evidence["restore_verified"])
        self.assertEqual(evidence["baseline_record_sha256"], hashlib.sha256(self.before_path.read_bytes()).hexdigest())
        ledger = evidence["patch_ledger"]
        self.assertEqual(ledger["qualification"], "draft-unqualified")
        before_evidence = self.before["compiler_evaluation"]
        self.assertEqual(ledger["patches"][:6], before_evidence["patch_ledger"]["patches"])
        for key in ("upstream_revision", "rust_revision"):
            self.assertEqual(ledger[key], before_evidence["patch_ledger"][key])
        self.assertEqual(len(ledger["patches"]), 7)
        proposal = ledger["patches"][-1]
        self.assertTrue(proposal["proposal"])
        path = root / "upstream/rust-llvm/proposals" / proposal["name"]
        self.assertEqual(proposal["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        spec = importlib.util.spec_from_file_location("aligned_compiler_patchset", root / "tools/apply-nuttx-patches.py")
        applicator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(applicator)
        series = applicator.SERIES["rust-llvm"]
        self.assertEqual(list(series), [entry["name"] for entry in ledger["patches"][:6]])
        self.assertNotIn(proposal["name"], series)
        self.assertEqual(evidence["backend_tests"], {"codegen_passed": 66, "mc_passed": 36, "total_passed": 102})
        driver = evidence["compiler"]["compiler_libraries_sha256"]
        self.assertNotEqual(driver, self.before["compiler_evaluation"]["compiler"]["compiler_libraries_sha256"])
        self.assertTrue(driver)
        for digest in driver.values():
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(set(evidence["rust_image_inputs"]), {"nuttx-rust-three"})
        inputs = evidence["rust_image_inputs"]["nuttx-rust-three"]
        self.assertEqual(inputs["std_source_inventory_sha256"],
                         before_evidence["rust_image_inputs"]["nuttx-rust-three"]["std_source_inventory_sha256"])
        self.assertRegex(inputs["elf_sha256"], r"^[0-9a-f]{64}$")
        for key in ("kernel_config_identity", "kernel_libraries_sha256"):
            self.assertEqual(inputs[key], before_evidence["rust_image_inputs"]["nuttx-rust-three"][key])
        for private in ('"raw_output"', "/Users/", "/home/", "usbmodem", "device-before.bin"):
            self.assertNotIn(private, self.text)


if __name__ == "__main__":
    unittest.main()
