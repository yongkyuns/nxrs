import unittest

import build
from perfmon_results import parse_perfmon


class PerfmonBuildTests(unittest.TestCase):
    def test_counter_helper_is_opt_in_and_does_not_wrap_hot_calls(self):
        self.assertNotIn(build.HERE / "perfmon.c", build.source_inputs("rust"))
        self.assertIn(build.HERE / "perfmon.c", build.source_inputs("rust", perfmon=True))
        flags = build.make_variables("compiler-", "c", [], perfmon=True)
        self.assertIn("EXTRALINKCMDS=--wrap=nxrs_sq_ready --wrap=nxrs_cq_thread_join", flags)

    def test_derived_linker_script_places_padding_before_flash_code(self):
        text = "    _instruction_reserved_start = ABSOLUTE(.);\n    *(.literal .text .text.*)\n"
        result = build.diagnostic_sections(text, padding=True)
        self.assertLess(result.index("KEEP(*(.text.nxrs_sq_layout_padding))"), result.index("*(.literal .text"))
        self.assertNotIn("KEEP", text)
        with self.assertRaises(ValueError):
            build.diagnostic_sections(text + text, padding=True)

    def test_iram_treatment_reuses_input_sections_without_changing_instructions(self):
        text = "    /* Code marked as running out of IRAM */\n    *(.iram1 .iram1.*)\n"
        result = build.diagnostic_sections(text, hot_iram=True)
        for selector in build.HOT_IRAM_SELECTORS:
            self.assertIn(selector, result)
        self.assertIn("*(.literal.nxrs_sq_worker .text.nxrs_sq_worker)", result)
        with self.assertRaises(ValueError):
            build.diagnostic_sections("", hot_iram=True)

    def test_counter_protocol_rejects_missing_duplicate_wrong_and_overflow(self):
        row = "SQ_PM mode=fetch select0=4 mask0=35 value0=123 select1=5 mask1=32 value1=5 overflow=0"
        self.assertEqual(parse_perfmon(row, mode="fetch")["value1"], 5)
        for bad in ("", row + "\n" + row, row.replace("mask0=35", "mask0=33"),
                    row.replace("overflow=0", "overflow=1"), row + " extra=1",
                    row.replace("value0=123", "value0=4294967296")):
            with self.subTest(row=bad), self.assertRaises(ValueError):
                parse_perfmon(bad, mode="fetch")


if __name__ == "__main__":
    unittest.main()
