"""Mocked serial and parser tests for the event-service device harness."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("event_measure_tests", Path(__file__).with_name("measure.py"))
measure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(measure)


def raw_output(profile="normal", platform="embassy", layout="one", *,
               rejected=0, missed=0, malformed=None):
    attempted = measure.EXPECTED_ATTEMPTS[profile]
    accepted = attempted - rejected
    queues = 60 if layout == "three" else 20
    capacity_ok = int(rejected == 0)
    deadlines_ok = int(missed == 0)
    service_rows = []
    for service_id in range(20):
        service_rows.append(
            f"ES_SERVICE id={service_id} received={accepted // 20} start_p99_us=6 "
            f"start_max_us=10 finish_p99_us=12 control_p99_us=4 "
            f"queue_p99_us=3 queue_max_us=7 "
            f"missed_control={missed // 20} missed_data=0 missed_status=0 "
            f"rejected={rejected // 20}"
        )
    result = {
        "platform": platform, "profile": profile, "services": 20, "queues": queues,
        "event_bytes": 64, "slots": 480, "attempted": attempted,
        "accepted": accepted, "received": accepted, "rejected": rejected,
        "errors": 0, "missed": missed, "publication_p99_us": 3,
        "publication_max_us": 4, "start_p99_us": 6, "start_max_us": 10,
        "finish_p99_us": 12, "finish_max_us": 20, "control_p99_us": 4,
        "control_max_us": 6, "worst_service_p99_us": 6,
        "queue_p99_us": 4, "queue_max_us": 9,
        "queue_peak_observed": 8 if layout == "three" else 24,
        "last_finish_us": 2_500_000, "delivery_ok": 1,
        "capacity_ok": capacity_ok, "deadlines_ok": deadlines_ok,
    }
    if malformed:
        if malformed == "service-number":
            service_rows[0] = service_rows[0].replace("received=", "received=x", 1)
        elif malformed == "missing-service":
            service_rows.pop()
        elif malformed == "service-queue-order":
            service_rows[0] = service_rows[0].replace("queue_p99_us=3", "queue_p99_us=8")
        elif malformed == "result-queue-order":
            result["queue_p99_us"] = 10
        elif malformed.startswith("result-"):
            key = malformed.split("-", 1)[1]
            result[key] = "bad"
    lines = [*service_rows,
             "ES_RESOURCES queue_buffers=30720 queue_objects=800 thread_objects=400 "
             "entry_storage=300 stack_storage=8192 heap_allocated=0 "
             "diagnostics_queue_peaks=80 note=static",
             "ES_MEMORY application_state=3000 diagnostics=10000 heap_before=100 heap_live=200",
             "ES_RESULT " + " ".join(f"{key}={value}" for key, value in result.items()),
             "ES_PASS", "ES_COMMAND_EXIT status=0"]
    return "\n".join(lines) + "\n"


class MeasurementValidationTests(unittest.TestCase):
    def test_all_profiles_and_layouts_validate_expected_workloads(self):
        for profile in measure.PROFILES:
            for layout in measure.LAYOUTS:
                parsed = measure.validate_output(raw_output(profile, layout=layout), profile, layout, "embassy")
                self.assertEqual(parsed["result"]["attempted"], measure.EXPECTED_ATTEMPTS[profile])

    def test_capacity_and_deadline_failures_remain_valid_protocol_data(self):
        parsed = measure.validate_output(
            raw_output("overload", layout="three", rejected=120, missed=20),
            "overload", "three", "embassy")
        self.assertEqual(parsed["result"]["capacity_ok"], 0)
        self.assertEqual(parsed["result"]["deadlines_ok"], 0)
        self.assertEqual(parsed["result"]["delivery_ok"], 1)

    def test_rejects_malformed_numeric_protocol_and_topology_rows(self):
        for malformed in ("service-number", "missing-service", "result-attempted"):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                measure.validate_output(raw_output(malformed=malformed), "normal", "one", "embassy")
        bad_platform = raw_output().replace("platform=embassy", "platform=zephyr-c")
        with self.assertRaisesRegex(ValueError, "platform/profile"):
            measure.validate_output(bad_platform, "normal", "one", "embassy")

    def test_rejects_queue_peak_and_profile_attempt_count_mismatch(self):
        text = raw_output().replace("queue_peak_observed=24", "queue_peak_observed=25")
        with self.assertRaisesRegex(ValueError, "queue peak"):
            measure.validate_output(text, "normal", "one", "embassy")
        record = {"contract_version": 2, "failure": None, "completed_runs": 1, "requested_runs": 1,
                  "image_sha256": "abc", "runs": []}
        with self.assertRaisesRegex(ValueError, "incomplete"):
            measure.verify_measurement_record(record, image_sha256="abc", platform="embassy",
                                              layout="one", profiles=["normal", "burst"], runs=1)

    def test_queue_metrics_are_optional_only_for_explicit_legacy_parse(self):
        text = raw_output().replace(" queue_p99_us=3 queue_max_us=7", "")
        text = text.replace(" queue_p99_us=4 queue_max_us=9", "")
        with self.assertRaisesRegex(ValueError, "queue-start latency metrics are required"):
            measure.validate_output(text, "normal", "one", "embassy")
        parsed = measure.validate_output(text, "normal", "one", "embassy",
                                         require_queue_metrics=False)
        self.assertEqual(parsed["result"]["attempted"], 3900)
        legacy_record = {"contract_version": 1, "failure": None, "completed_runs": 1,
                         "requested_runs": 1, "image_sha256": "abc", "runs": []}
        with self.assertRaisesRegex(ValueError, "contract version"):
            measure.verify_measurement_record(
                legacy_record, image_sha256="abc", platform="embassy", layout="one",
                profiles=["normal"], runs=1)
        for malformed in ("service-queue-order", "result-queue-order"):
            with self.subTest(malformed=malformed), self.assertRaisesRegex(ValueError, "queue p99"):
                measure.validate_output(raw_output(malformed=malformed), "normal", "one", "embassy")

    def test_ansi_and_exact_leading_nsh_prompt_do_not_hide_service_marker(self):
        text = raw_output(layout="three")
        first, remainder = text.split("\n", 1)
        prefixed = "\x1b[32mnsh> " + first + "\x1b[0m\r\n" + remainder
        parsed = measure.validate_output(prefixed.encode(), "normal", "three", "embassy")
        self.assertEqual(len(parsed["services"]), 20)
        contaminated = "banner nsh> " + first + "\n" + remainder
        with self.assertRaisesRegex(ValueError, "ES_SERVICE: expected 20 rows, found 19"):
            measure.validate_output(contaminated, "normal", "three", "embassy")

    def test_profile_order_rotates_per_run(self):
        self.assertEqual(measure.rotated_profiles(["normal", "burst", "overload"], 0),
                         ["normal", "burst", "overload"])
        self.assertEqual(measure.rotated_profiles(["normal", "burst", "overload"], 1),
                         ["burst", "overload", "normal"])

    def test_free_memory_requires_a_consistent_numeric_snapshot(self):
        memory = {"total": 1000, "used": 400, "free": 600, "maxused": 500,
                  "maxfree": 700, "nused": 4, "nfree": 5}
        self.assertEqual(measure.validate_free_memory(memory), memory)
        with self.assertRaisesRegex(ValueError, "sum to total"):
            measure.validate_free_memory({**memory, "free": 599})

    def test_measurement_record_reparses_raw_and_rejects_tampering(self):
        text = raw_output()
        parsed = measure.validate_output(text, "normal", "one", "embassy")
        entry = {"profile": "normal", **parsed, "raw_output": text,
                 "raw_output_sha256": hashlib.sha256(text.encode()).hexdigest(),
                 "free_raw_output": "", "free_memory": None}
        record = {"contract_version": 2, "failure": None, "completed_runs": 1, "requested_runs": 1,
                  "image_sha256": "abc", "runs": [entry]}
        checked = measure.verify_measurement_record(record, image_sha256="abc", platform="embassy",
                                                    layout="one", profiles=["normal"], runs=1)
        self.assertEqual(checked[0]["result"]["attempted"], 3900)
        self.assertIsNone(checked[0]["free_memory"])
        record["runs"][0]["result"]["accepted"] = -1
        with self.assertRaisesRegex(ValueError, "disagrees with raw"):
            measure.verify_measurement_record(record, image_sha256="abc", platform="embassy",
                                              layout="one", profiles=["normal"], runs=1)


class MockedDeviceTests(unittest.TestCase):
    def test_measure_flashes_once_captures_each_run_and_keeps_header(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image, output = root / "image.bin", root / "out"
            image.write_bytes(b"frozen firmware")
            argv = ["measure.py", "--image", str(image), "--platform", "embassy",
                    "--layout", "one", "--port", "PORT", "--flasher", "FLASHER",
                    "--out", str(output), "--runs", "1", "--profile", "normal"]
            serial_data = raw_output().encode()
            with patch.object(measure.sys, "argv", argv), \
                 patch.object(measure.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], 0, "flashed", "")) as flash, \
                 patch.object(measure.shared, "open_serial", return_value=71), \
                 patch.object(measure, "read_until_prompt", return_value=b"event> "), \
                 patch.object(measure, "read_until_completion", return_value=serial_data), \
                 patch.object(measure.os, "write"), patch.object(measure.os, "close"):
                measure.main()
            self.assertEqual(flash.call_count, 1)
            args = flash.call_args.args[0]
            for option in ("--flash_mode", "--flash_freq", "--flash_size"):
                self.assertEqual(args[args.index(option) + 1], "keep")
            report = json.loads((output / "measurement.json").read_text())
            self.assertEqual(report["contract_version"], 2)
            self.assertEqual(report["completed_runs"], 1)
            self.assertEqual(report["runs"][0]["raw_output"], raw_output())
            self.assertEqual((output / "measurement.json").stat().st_mode & 0o777, 0o600)

    def test_nuttx_command_is_byte_paced_and_free_parse_is_optional(self):
        firmware_output = raw_output(platform="nuttx-c", layout="three").replace(
            "ES_COMMAND_EXIT status=0\n", "")
        with patch.object(measure, "_send_paced") as send, \
             patch.object(measure, "read_until_prompt",
                          side_effect=[firmware_output.encode(),
                                       b"then echo ES_COMMAND_EXIT status=0\r\nES_COMMAND_EXIT status=0\r\nnsh> ",
                                       b"else echo ES_COMMAND_EXIT status=1\r\nnsh> ",
                                       b"fi\r\nnsh> ", b"free output without Umem"]), \
             patch.object(measure.footprint, "parse_free", side_effect=ValueError("no Umem row")):
            entry = measure.run_one(73, "normal", "three", "nuttx-c")
        self.assertEqual([call.args[1] for call in send.call_args_list], [
            "if es_c normal", "then echo ES_COMMAND_EXIT status=0",
            "else echo ES_COMMAND_EXIT status=1", "fi", "free"])
        self.assertEqual(entry["free_memory"], None)
        self.assertEqual(entry["free_raw_output"], "free output without Umem")

    def test_shell_free_nuttx_uses_event_transport_without_free_command(self):
        output = raw_output(platform="nuttx-c", layout="three")
        memory = b"1000 200 800 300 500 2 3 Umem\nevent> "
        with patch.object(measure.os, "write") as write, \
             patch.object(measure, "read_until_completion", return_value=output.encode()) as read, \
             patch.object(measure, "read_until_prompt", return_value=memory), \
             patch.object(measure, "_send_paced") as shell:
            entry = measure.run_one(73, "normal", "three", "nuttx-c", "event")
        self.assertEqual([call.args for call in write.call_args_list], [(73, b"normal\r"), (73, b"memory\r")])
        read.assert_called_once_with(73, b"event> ", 120)
        shell.assert_not_called()
        self.assertEqual(entry["free_raw_output"], memory.decode())
        self.assertEqual(entry["free_memory"]["maxused"], 300)

    def test_measurement_cannot_claim_a_different_console(self):
        with self.assertRaisesRegex(ValueError, "console identity"):
            measure.verify_measurement_record({"nuttx_console": "event"}, image_sha256="abc",
                platform="nuttx-c", layout="three", profiles=["normal"], runs=1)

    def test_failed_minimal_memory_observation_preserves_both_raw_outputs(self):
        output = raw_output(platform="nuttx-c", layout="three").encode()
        memory = b"invalid memory\nevent> "
        with patch.object(measure.os, "write"), \
             patch.object(measure, "read_until_completion", return_value=output), \
             patch.object(measure, "read_until_prompt", return_value=memory):
            with self.assertRaises(measure.MeasurementOutputError) as failure:
                measure.run_one(73, "normal", "three", "nuttx-c", "event")
        self.assertEqual(failure.exception.raw_output, output)
        self.assertEqual(failure.exception.free_raw_output, memory)

    def test_failed_validation_retains_raw_bytes_through_main_finally(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image, output = root / "image.bin", root / "out"
            image.write_bytes(b"frozen firmware")
            argv = ["measure.py", "--image", str(image), "--platform", "embassy",
                    "--layout", "one", "--port", "PORT", "--flasher", "FLASHER",
                    "--out", str(output), "--runs", "1", "--profile", "normal"]
            failed = b"\x1b[31mES_FAIL stage=protocol\r\n"
            error = measure.MeasurementOutputError("firmware reported ES_FAIL", failed)
            with patch.object(measure.sys, "argv", argv), \
                 patch.object(measure.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], 0, "flashed", "")), \
                 patch.object(measure.shared, "open_serial", return_value=71), \
                 patch.object(measure, "read_until_prompt", return_value=b"event> "), \
                 patch.object(measure, "run_one", side_effect=error) as run_one, \
                 patch.object(measure.os, "close"):
                with self.assertRaises(measure.MeasurementOutputError):
                    measure.main()
            run_one.assert_called_once()
            report = json.loads((output / "measurement.json").read_text())
            self.assertEqual(report["failure_raw_output"], failed.decode("latin-1"))
            self.assertIn(failed, (output / "serial.log").read_bytes())

    def test_completion_timeout_retains_every_partial_byte(self):
        with patch.object(measure.time, "monotonic", side_effect=[0, 0, 0, 2]), \
             patch.object(measure.select, "select", return_value=([71], [], [])), \
             patch.object(measure.os, "read", return_value=b"first\x00partial"):
            with self.assertRaises(measure.SerialReadTimeout) as caught:
                measure.read_until_completion(71, b"event> ", timeout=1)
        self.assertEqual(caught.exception.raw_output, b"first\x00partial")

    def test_event_completion_timeout_becomes_retained_measurement_failure(self):
        partial = b"ES_SERVICE id=0 partial output"
        with patch.object(measure.os, "write"), \
             patch.object(measure, "read_until_completion",
                          side_effect=measure.SerialReadTimeout("timeout", partial)):
            with self.assertRaises(measure.MeasurementOutputError) as caught:
                measure.run_one(71, "normal", "one", "embassy")
        self.assertEqual(caught.exception.raw_output, partial)

    def test_boot_prompt_timeouts_are_fully_logged_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image, output = root / "image.bin", root / "out"
            image.write_bytes(b"frozen firmware")
            argv = ["measure.py", "--image", str(image), "--platform", "embassy",
                    "--layout", "one", "--port", "PORT", "--flasher", "FLASHER",
                    "--out", str(output), "--runs", "1", "--profile", "normal"]
            first, second = b"boot bytes 1", b"boot bytes 2"
            with patch.object(measure.sys, "argv", argv), \
                 patch.object(measure.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], 0, "flashed", "")), \
                 patch.object(measure.shared, "open_serial", return_value=71), \
                 patch.object(measure, "read_until_prompt",
                              side_effect=[measure.SerialReadTimeout("first", first),
                                           measure.SerialReadTimeout("second", second)]), \
                 patch.object(measure.os, "write"), patch.object(measure.os, "close"), \
                 patch.object(measure, "run_one") as run_one:
                with self.assertRaises(measure.MeasurementOutputError):
                    measure.main()
            run_one.assert_not_called()
            record = json.loads((output / "measurement.json").read_text())
            self.assertEqual(record["failure_raw_output"], (first + second).decode("latin-1"))
            self.assertEqual((output / "serial.log").read_bytes().count(first), 1)
            self.assertIn(second, (output / "serial.log").read_bytes())


if __name__ == "__main__":
    unittest.main()
