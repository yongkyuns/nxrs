"""Protect the distinction between reservations, test storage and mixed runtime."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_controls import report

HERE = Path(__file__).resolve().parent
ram = report.load("event_ram_attribution", HERE / "ram_attribution.py")
CONFIG = """CONFIG_MQ_MAXMSGSIZE=248
CONFIG_MM_DEFAULT_ALIGNMENT=8
CONFIG_MM_NODE_GUARDSIZE=0
CONFIG_MM_BACKTRACE=-1
CONFIG_PREALLOC_MQ_MSGS=8
"""


def replay_inputs(row):
    # Disjoint synthetic addresses replay the pure arithmetic from the committed
    # selected-object manifest; the CLI separately verifies real ELF intervals.
    parts = row["components"]
    static = row["whole_ram_peak_bytes"] - parts["live_heap_not_split"] - parts["heap_peak_margin"]
    if row["details"].get("dynamic_message_contents"):
        static -= parts["execution_workspace"]
        static -= row["details"]["dynamic_message_contents"] + row["details"]["dynamic_message_overhead"]
    sections = [dict(name=".code", address=0x40370000, size=parts["ram_code"],
                     type="PROGBITS", flags="AX"),
                dict(name=".data", address=0x3FC80000, size=static-parts["ram_code"],
                     type="NOBITS", flags="WA")]
    symbols, address = [], 0x3FC80000
    for name, size in row["object_bytes"].items():
        symbols.append((address, size, name))
        address += size + 8
    return sections, symbols


class RamAttributionTests(unittest.TestCase):
    def setUp(self):
        self.record = json.loads((HERE / "results/esp32s3-instrumentation-2026-10-10.json").read_text())

    def test_every_ledger_reconciles_and_replays_without_optional_sdks(self):
        for cohort, cases in self.record["ram_attribution"]["cohorts"].items():
            for name, row in cases.items():
                case = self.record[cohort]["cases"][name]
                with self.subTest(cohort=cohort, case=name):
                    self.assertEqual(row["elf_sha256"], case["elf_sha256"])
                    self.assertEqual(sum(row["components"].values()), case["flash_and_ram"]["whole_ram_peak_bytes"])
                    sections, symbols = replay_inputs(row)
                    self.assertEqual(ram.attribute(case, sections, symbols, CONFIG), row)

    def test_histograms_are_the_only_full_lean_attribution_change(self):
        ledger = self.record["ram_attribution"]["cohorts"]
        for name, full in ledger["reference"].items():
            expected = dict(full["components"])
            expected["known_test_storage"] -= 26112
            self.assertEqual(expected, ledger["lean"][name]["components"])

    def test_application_state_is_not_checker_overhead(self):
        for cohort, cases in self.record["ram_attribution"]["cohorts"].items():
            for name, row in cases.items():
                parts = row["components"]
                self.assertEqual(parts["fixture_application_state"], 3040)
                lean_overhead = 5059 if name == "embassy-three" else (
                    4432 if name == "zephyr-c-three" else 4424)
                self.assertEqual(parts["known_test_storage"], lean_overhead +
                                 (26112 if cohort == "reference" else 0))
        row = self.record["ram_attribution"]["cohorts"]["lean"]["nuttx-c-three"]
        damaged = copy.deepcopy(self.record["lean"]["cases"]["nuttx-c-three"])
        damaged["profiles"]["normal"]["memory_metrics"]["application_state"]["max"] += 4
        with self.assertRaisesRegex(ValueError, "application-state"):
            ram.attribute(damaged, *replay_inputs(row), CONFIG)

    def test_nuttx_static_pool_contents_are_not_counted_again_in_heap(self):
        row = self.record["ram_attribution"]["cohorts"]["lean"]["nuttx-c-three"]
        d = row["details"]
        self.assertEqual(d["pool_event_contents"], 512)
        self.assertEqual(d["dynamic_message_contents"], 472 * 64)
        self.assertEqual(d["dynamic_message_overhead"], 472 * 16)
        self.assertEqual(d["pool_spare_and_headers"] + d["pool_event_contents"], d["fixed_message_pool"])
        case = copy.deepcopy(self.record["lean"]["cases"]["nuttx-c-three"])
        case["profiles"]["saturation"]["runs"][0]["cycles"][0]["full_heap"] += 8
        with self.assertRaisesRegex(ValueError, "allocation model"):
            ram.attribute(case, *replay_inputs(row), CONFIG)

    def test_allocator_and_unstable_heap_assumptions_fail_closed(self):
        row = self.record["ram_attribution"]["cohorts"]["lean"]["nuttx-c-three"]
        case = self.record["lean"]["cases"]["nuttx-c-three"]
        with self.assertRaisesRegex(ValueError, "allocator layout"):
            ram.attribute(case, *replay_inputs(row), CONFIG.replace("BACKTRACE=-1", "BACKTRACE=0"))
        damaged = copy.deepcopy(case)
        damaged["profiles"]["saturation"]["runs"][0]["cycles"][0]["drained_heap"] += 8
        with self.assertRaisesRegex(ValueError, "stable/reversible"):
            ram.attribute(damaged, *replay_inputs(row), CONFIG)

    def test_symbol_aliases_markers_and_flash_objects_are_not_storage(self):
        sections = [dict(name=".bss", address=0x3FC80000, size=100, flags="WA")]
        output = """
1: 3fc80000 16 OBJECT LOCAL DEFAULT 2 data
2: 3fc80000 16 OBJECT GLOBAL DEFAULT ABS alias
3: 3fc80010 0 NOTYPE GLOBAL DEFAULT 2 marker
4: 42000000 16 OBJECT GLOBAL DEFAULT 3 flash
"""
        symbols = ram.objects(output, sections, [".bss"])
        self.assertEqual(symbols, [(0x3FC80000, 16, "data")])
        ledger = ram.Ledger(symbols + [(0x3FC80008, 16, "overlap")])
        self.assertEqual(ledger.take("data"), 16)
        for name in ("data", "overlap", "missing"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                ledger.take(name)

    def test_frozen_elf_identity_is_checked_before_running_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "app.elf").write_bytes(b"wrong image")
            (directory / "build-provenance.json").write_text(json.dumps(
                {"artifacts": {"app.elf": "0" * 64}}))
            case = self.record["lean"]["cases"]["nuttx-c-three"]
            with patch.object(ram.subprocess, "check_output") as command:
                with self.assertRaisesRegex(ValueError, "ELF identity"):
                    ram.frozen_attribution(case, directory, "readelf")
                command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
