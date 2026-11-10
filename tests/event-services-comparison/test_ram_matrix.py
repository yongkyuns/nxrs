"""Fresh cohort validation, serial failure capture and non-overlapping accounting."""
import copy
import hashlib
import importlib.util
from pathlib import Path
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
        free = {"maxused": 5000}
        with patch.object(ram.os, "write"), \
             patch.object(ram.measure, "_read_serial_until", return_value=raw), \
             patch.object(ram.measure, "capture_free_memory", return_value=(b"free", free)):
            result = ram.capture(42, "alloc-threads-1", "nuttx-c", "three", provenance()["configuration"])
        self.assertEqual(result["raw_output_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result["free_memory"], free)
        self.assertEqual(result["live_heap_delta_from_baseline"]["threads"], 2400)


if __name__ == "__main__":
    unittest.main()
