"""Fresh cohort validation, serial failure capture and non-overlapping accounting."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ram = load("fresh_ram_matrix_tests", "ram_matrix.py")
fixtures = load("fresh_ram_fixture_tests", "test_controls.py")
stacks = load("fresh_ram_stack_fixture_tests", "test_ram_contract.py")


def provenance(platform="nuttx-c"):
    sections = [dict(name=".text", type="PROGBITS", size=400, address=0x40374000, flags="AX"),
                dict(name=".bss", type="NOBITS", size=1000, address=0x3FC80000, flags="WA")]
    return dict(platform=platform, layout="three", elf_sections=sections,
                section_accounting=ram.esp_idf_section_accounting(sections), configuration=dict(
                    ram_contract=1, stack_watermark=True, psram=False, cores=1, slots=480,
                    event_bytes=64, queues=60, worker_stack_bytes=2048, shared_stack_bytes=4096,
                    instrumentation="full", console="event", publication_timer_resolution_ms=1,
                    embassy_scheduling="natural" if platform == "embassy" else "native",
                    work_mode="monolithic"))


class RamMatrixTests(unittest.TestCase):
    def test_fresh_stack_builds_reject_old_or_external_memory_evidence(self):
        original = provenance()
        self.assertEqual(ram.validate_configuration(original), original["configuration"])
        for edits in ({"ram_contract": 0}, {"stack_watermark": False}, {"psram": True},
                      {"cores": 2}, {"cores": True}, {"slots": 240}, {"queues": 20},
                      {"worker_stack_bytes": 2049}):
            item = copy.deepcopy(original)
            item["configuration"].update(edits)
            with self.subTest(edits=edits), self.assertRaises(ValueError):
                ram.validate_configuration(item)
        for edit in ("address", "total"):
            item = copy.deepcopy(original)
            if edit == "address":
                item["elf_sections"][1]["address"] = 0x3D000000
            else:
                item["section_accounting"]["resident_ram_bytes"] = 1
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                ram.validate_configuration(item)

    def test_saturation_uses_frozen_sized_stacks_and_requires_all_observations(self):
        config = provenance()["configuration"]
        text = fixtures.saturation_output("nuttx-c", "three").replace("81920", "40960")
        text += "\n".join(stacks.worker(i) for i in range(20))
        checked = ram.validate_output(text.encode(), "saturation", "nuttx-c", "three", config)
        self.assertEqual(len(checked["stacks"]), 20)
        self.assertEqual(checked["result"]["stacks"], 40960)
        for damaged in (text.replace(stacks.worker(19), ""), text.replace("stacks=40960", "stacks=81920")):
            with self.assertRaises(ValueError):
                ram.validate_output(damaged.encode(), "saturation", "nuttx-c", "three", config)

    def test_traffic_cannot_silently_use_a_different_timer_or_scheduler(self):
        config = provenance()["configuration"]
        text = fixtures.traffic_output(platform="nuttx-c", layout="three")
        text += "ES_SCHED policy=native work_mode=monolithic\n"
        text += "\n".join(stacks.worker(i) for i in range(20))
        ram.validate_output(text.encode(), "normal", "nuttx-c", "three", config)
        for old, new in (("timer_ms=1", "timer_ms=10"), ("policy=native", "policy=natural")):
            with self.assertRaisesRegex(ValueError, "configuration"):
                ram.validate_output(text.replace(old, new).encode(), "normal", "nuttx-c", "three", config)

    def test_totals_do_not_add_stacks_or_static_arenas_twice(self):
        runs = [{"free_memory": {"maxused": 10000}}, {"free_memory": {"maxused": 12000}}]
        native = ram.image_summary(provenance(), runs)
        self.assertEqual(native["whole_image_observed_peak_bytes"], 13400)
        self.assertEqual(native["ram_code_and_vectors_bytes"], 400)
        self.assertFalse(native["stack_reservations_added_again"])
        self.assertIn("cumulative", native["peak_scope"])
        for platform in ("zephyr-c", "embassy"):
            self.assertEqual(ram.image_summary(provenance(platform), [{"free_memory": None}])[
                "whole_image_observed_peak_bytes"], 1400)
        self.assertIsNone(ram.image_summary(provenance(), [{"free_memory": None}])[
            "whole_image_observed_peak_bytes"])

    def test_halted_device_failure_does_not_wait_for_a_prompt(self):
        failed = b"ES_FAIL stage=stack_observation\nES_COMMAND_EXIT status=1\n"

        def read(fd, timeout, predicate, label):
            self.assertTrue(predicate(failed))
            return failed

        with patch.object(ram.os, "write"), patch.object(ram.measure, "_read_serial_until", side_effect=read):
            with self.assertRaises(ram.measure.MeasurementOutputError) as raised:
                ram.capture(42, "normal", "embassy", "three", provenance("embassy")["configuration"])
        self.assertEqual(raised.exception.raw_output, failed)

    def test_allocation_capture_retains_exact_raw_hash_and_snapshot(self):
        raw = (stacks.allocation("alloc-threads-1") + "\nevent> ").encode()
        free = {"maxused": 5000, "used": 4000}
        with patch.object(ram.os, "write"), \
             patch.object(ram.measure, "_read_serial_until", return_value=raw), \
             patch.object(ram.measure, "capture_free_memory", return_value=(b"free", free)):
            result = ram.capture(42, "alloc-threads-1", "nuttx-c", "three", provenance()["configuration"])
        self.assertEqual(result["raw_output_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result["free_memory"], free)
        self.assertEqual(result["live_heap_delta_from_baseline"]["threads"], 2400)

    def test_failed_heap_capture_preserves_each_stream_once(self):
        raw = (stacks.allocation("alloc-baseline") + "\nevent> ").encode()
        damaged = b"incomplete heap\nevent> "
        failure = ram.measure.MeasurementOutputError("invalid heap", raw, damaged)
        with patch.object(ram.os, "write"), \
             patch.object(ram.measure, "_read_serial_until", return_value=raw), \
             patch.object(ram.measure, "capture_free_memory", side_effect=failure):
            with self.assertRaises(ram.measure.MeasurementOutputError) as raised:
                ram.capture(42, "alloc-baseline", "nuttx-c", "three", provenance()["configuration"])
        self.assertEqual(raised.exception.raw_output, raw)
        self.assertEqual(raised.exception.free_raw_output, damaged)

    def test_collection_retains_failed_tail_and_restores_without_success_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts, output = root / "images", root / "run"
            frozen_case(artifacts)
            args = ["ram_matrix.py", "--artifacts", str(artifacts), "--out", str(output),
                    "--readelf", "readelf", "--backup", str(root / "backup.bin"),
                    "--port", "test-port", "--flasher", "test-flasher",
                    "--case", "nuttx-c-three", "--runs", "1"]
            boot, raw, free_raw = b"boot\nevent> ", b"successful invocation\n", b"FATAL ERROR heap\n"
            failure = ram.measure.MeasurementOutputError("late heap failure", raw, free_raw)
            flashed = ram.subprocess.CompletedProcess([], 0, "flashed", "")
            with patch("sys.argv", args), \
                 patch.object(ram.subprocess, "check_output", return_value=SECTION_TEXT), \
                 patch.object(ram.subprocess, "run", return_value=flashed), \
                 patch.object(ram.matrix, "backup_identity", return_value="b" * 64), \
                 patch.object(ram.matrix, "restore") as restored, \
                 patch.object(ram.measure.shared, "open_serial", return_value=42), \
                 patch.object(ram.os, "write"), patch.object(ram.os, "close"), \
                 patch.object(ram.measure, "read_until_prompt", return_value=boot), \
                 patch.object(ram, "capture", side_effect=failure):
                with self.assertRaisesRegex(ram.measure.MeasurementOutputError, "late heap failure"):
                    ram.main()
                restored.assert_called_once()
            record_path = output / "ram-matrix.json"
            record = json.loads(record_path.read_text())
            self.assertTrue(record["restore_verified"])
            self.assertIn("late heap failure", record["failure"])
            self.assertEqual(record["cases"][0]["runs"], [])
            self.assertEqual((output / "nuttx-c-three-serial.log").read_bytes(), boot + raw + free_raw)
            with self.assertRaisesRegex(ValueError, "failed, incomplete"):
                ram.verify_record(record_path, artifacts, "readelf")


SECTION_TEXT = """  [ 1] .text PROGBITS 40374000 001000 000190 00 AX 0 0 4
  [ 2] .bss NOBITS 3fc80000 002000 0003e8 00 WA 0 0 4
"""


def frozen_case(root):
    case = root / "nuttx-c-three"
    case.mkdir(parents=True)
    for name, value in (("app.elf", "ELF test fixture"), ("image.bin", "image test fixture"),
                        ("resolved.config", "CONFIG_BUILD_FLAT=y\nCONFIG_STACK_COLORATION=y\nCONFIG_USEC_PER_TICK=1000\n")):
        (case / name).write_text(value)
    proof = provenance()
    sections = ram.parse_sections(SECTION_TEXT)
    proof.update(status="success", failure=None, image_file="image.bin", elf_file="app.elf",
                 elf_sections=sections, section_accounting=ram.esp_idf_section_accounting(sections),
                 artifacts={path.name: ram.digest(path.read_bytes()) for path in case.iterdir()},
                 source_sha256={"platform_nuttx.c": "a" * 64},
                 kernel_config_identity=ram.nuttx_config.helpers.config_identity(case / "resolved.config"))
    (case / "build-provenance.json").write_text(json.dumps(proof))
    return proof


def matrix_record(root, artifacts):
    proof = frozen_case(artifacts)
    config = proof["configuration"]
    runs = []
    boot = b"boot complete\nevent> "
    serial = bytearray(boot)
    for profile in ram.profiles_for("nuttx-c"):
        if profile in ram.contract.ALLOC_PROFILES:
            text = stacks.allocation(profile)
        else:
            if profile == "saturation":
                text = fixtures.saturation_output("nuttx-c", "three").replace("81920", "40960")
            else:
                text = fixtures.traffic_output(profile, "nuttx-c", "three")
                text += "ES_SCHED policy=native work_mode=monolithic\n"
            text += "\n".join(stacks.worker(i) for i in range(20))
        raw = (text + "\nevent> ").encode()
        free_raw = b"100000 10000 90000 12000 80000 20 3 Umem\nevent> "
        free = ram.measure.validate_free_memory(ram.measure.footprint.parse_free(free_raw))
        runs.append({"profile": profile, **ram.validate_output(raw, profile, "nuttx-c", "three", config),
                     "raw_output": raw.decode("latin-1"), "raw_output_sha256": ram.digest(raw),
                     "free_raw_output": free_raw.decode("latin-1"), "free_raw_output_sha256": ram.digest(free_raw),
                     "free_memory": free})
        serial.extend(raw + free_raw)
    entry = {"case": "nuttx-c-three", "provenance": proof, "runs": runs,
             "boot_output": boot.decode(), "boot_output_sha256": ram.digest(boot),
             "serial_file": "nuttx-c-three-serial.log", "serial_sha256": ram.digest(serial),
             "memory_summary": ram.image_summary(proof, runs)}
    (root / entry["serial_file"]).write_bytes(serial)
    record = dict(schema=2, kind="event-services-ram-controls", environment="physical-esp32s3",
                  backup_sha256="b" * 64, requested_cases=["nuttx-c-three"], runs_per_case=1,
                  cases=[entry], first_use_measurement=False, failure=None,
                  restore_verified=True, restore_error=None)
    return record


class RamReplayTests(unittest.TestCase):
    def test_preflight_cli_requires_no_capture_or_device(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen_case(root)
            output = root / "preflight.json"
            args = ["ram_matrix.py", "--preflight", "--artifacts", str(root), "--readelf", "readelf",
                    "--case", "nuttx-c-three", "--out", str(output)]
            with patch("sys.argv", args), \
                 patch.object(ram.subprocess, "check_output", return_value=SECTION_TEXT), \
                 patch.object(ram.measure.shared, "open_serial") as opened, \
                 patch.object(ram.matrix, "restore") as restored:
                ram.main()
            opened.assert_not_called()
            restored.assert_not_called()
            result = json.loads(output.read_text())
            self.assertFalse(result["device_io_performed"])
            self.assertFalse(result["physical_hardware_qualified"])
            self.assertEqual(result["cases"][0]["image_file"], "image.bin")

    def test_replay_binds_raw_numbers_complete_order_and_frozen_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = root / "images"
            record = matrix_record(root, artifacts)
            path = root / "ram-matrix.json"
            path.write_text(json.dumps(record))
            with patch.object(ram.subprocess, "check_output", return_value=SECTION_TEXT):
                result = ram.verify_record(path, artifacts, "readelf", expected_cases=["nuttx-c-three"], expected_runs=1)
            self.assertEqual(len(result["cases"][0]["runs"]), 13)
            self.assertEqual(result["cases"][0]["memory_summary"]["whole_image_observed_peak_bytes"], 13400)
            self.assertNotIn("backup_sha256", result)
            self.assertNotIn("raw_output", json.dumps(result))
            mutations = {
                "missing invocation": lambda r: r["cases"][0]["runs"].pop(),
                "reordered invocation": lambda r: r["cases"][0]["runs"].reverse(),
                "altered raw": lambda r: r["cases"][0]["runs"][0].update(raw_output="changed"),
                "altered numbers": lambda r: r["cases"][0]["runs"][0]["result"].update(received=0),
                "altered heap": lambda r: r["cases"][0]["runs"][0]["free_memory"].update(maxused=99999),
                "altered summary": lambda r: r["cases"][0]["memory_summary"].update(whole_image_observed_peak_bytes=1),
                "wrong build": lambda r: r["cases"][0]["provenance"]["configuration"].update(worker_stack_bytes=1024),
                "failed restore": lambda r: r.update(restore_verified=False),
                "missing restore status": lambda r: r.pop("restore_error"),
                "failed run": lambda r: r.update(failure="late device failure"),
                "missing case": lambda r: r.update(cases=[]),
            }
            for name, mutate in mutations.items():
                damaged = copy.deepcopy(record)
                mutate(damaged)
                path.write_text(json.dumps(damaged))
                with self.subTest(name=name), patch.object(ram.subprocess, "check_output", return_value=SECTION_TEXT):
                    with self.assertRaises(ValueError):
                        ram.verify_record(path, artifacts, "readelf")
            path.write_text(json.dumps(record))
            (root / record["cases"][0]["serial_file"]).write_text("truncated")
            with patch.object(ram.subprocess, "check_output", return_value=SECTION_TEXT):
                with self.assertRaisesRegex(ValueError, "serial file hash"):
                    ram.verify_record(path, artifacts, "readelf")

    def test_actual_elf_and_native_config_cannot_be_relabelled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proof = frozen_case(root)
            case = root / "nuttx-c-three"
            with patch.object(ram.subprocess, "check_output", return_value=SECTION_TEXT.replace("000190", "000194")):
                with self.assertRaisesRegex(ValueError, "ELF sections"):
                    ram.frozen_image(case, "nuttx-c-three", "readelf")
            (case / "resolved.config").write_text("CONFIG_ESP32S3_SPIRAM=y\n")
            with self.assertRaisesRegex(ValueError, "changed frozen artifact"):
                ram.frozen_image(case, "nuttx-c-three", "readelf")
            (case / "resolved.config").write_text("CONFIG_BUILD_FLAT=y\nCONFIG_STACK_COLORATION=y\nCONFIG_USEC_PER_TICK=1000\nCONFIG_TLS_NELEM=99\n")
            proof["artifacts"]["resolved.config"] = ram.digest((case / "resolved.config").read_bytes())
            (case / "build-provenance.json").write_text(json.dumps(proof))
            with patch.object(ram.subprocess, "check_output", return_value=SECTION_TEXT):
                with self.assertRaisesRegex(ValueError, "kernel configuration identity"):
                    ram.frozen_image(case, "nuttx-c-three", "readelf")

    def test_late_firmware_fault_is_rejected_even_with_success_markers(self):
        for tail in ("panic: teardown", "assertion failed: cleanup", "FATAL ERROR after command", "ES_FAIL stage=late"):
            text = stacks.allocation("alloc-baseline") + "\n" + tail
            with self.subTest(tail=tail), self.assertRaisesRegex(ValueError, "failure"):
                ram.validate_output(text.encode(), "alloc-baseline", "nuttx-c", "three", provenance()["configuration"])


if __name__ == "__main__":
    unittest.main()
