"""Literal stack-evidence controls; these host tests do not qualify a device."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_gdb_export as fixtures


exporter = fixtures.exporter
PATTERN = 0xDEADBEEF
ARM_MACROS = {
    "CONFIG_ARCH_ARM": 1, "CONFIG_ARCH_ARMV8M": 1,
    "CONFIG_STACK_COLORATION": 1, "CONFIG_SMP_NCPUS": 1,
    "STACK_COLOR": PATTERN, "STACKFRAME_ALIGN": 8,
}


class SavedPointer:
    def __init__(self, address):
        self.address = address
        self.type = SimpleNamespace(target=lambda: SimpleNamespace(sizeof=4))

    def __int__(self):
        return self.address


class StackTarget:
    def __init__(self, byteorder="little"):
        self.base = 0x1000
        self.data = bytearray(PATTERN.to_bytes(4, byteorder) * 64)
        self.reads = []
        self.byteorder = byteorder
        self.registers = {"xpsr": 0x01000000, "sp": 0x10C0}
        self.register_names = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc", "xpsr"]
        self.frame = SimpleNamespace(
            read_register=lambda name: self.registers[name],
            architecture=lambda: SimpleNamespace(registers=lambda: [SimpleNamespace(name=name) for name in self.register_names]),
        )
        self.task = fixtures.TargetPointer(0x800, {"task_state": 3, "xcp": {"regs": SavedPointer(0x1080)}})
        self.values = {
            "g_running_tasks": fixtures.TargetArray([self.task]), "TSTATE_TASK_RUNNING": 3,
            "g_tcbinfo": {"regs_num": 17, "reg_off": {"p": [4 * i for i in range(17)]}},
        }
        self.types = {"uint32_t": fixtures.TargetType(size=4)}
        self.thread_list = [SimpleNamespace(is_stopped=lambda: True)]

    def read_memory(self, address, size):
        self.reads.append((address, size))
        if address < self.base or address + size > self.base + len(self.data):
            raise RuntimeError("outside literal target memory")
        return memoryview(self.data)[address - self.base:address - self.base + size]

    def selected_inferior(self):
        return self

    def newest_frame(self):
        return self.frame

    def threads(self):
        return self.thread_list

    def selected_frame(self):
        raise AssertionError("Must not read an unwound selected frame")

    def parse_and_eval(self, expression):
        return self.values[expression]

    def lookup_type(self, name):
        return self.types[name]


def reader(target=None, macros=None):
    target = target or StackTarget()
    result = exporter.StackReader(
        target, {"host_elf": "/not-used/fixture.elf", "elf_sha256": "a" * 64,
                 "byte_order": target.byteorder},
        {"pointer_bits": 32, "address_limit": 1 << 32})
    result.constants = deepcopy(ARM_MACROS if macros is None else macros)
    result.loaded = True
    return result


class StackColorationTests(unittest.TestCase):
    def test_entirely_untouched_is_zero_not_full(self):
        target = StackTarget()
        before = bytes(target.data)
        result = reader(target).high_water(0x1000, 256)
        self.assertEqual(result, {"status": "coloration", "scan_start": 0x1000, "scan_size": 256,
                                  "used_bytes": 0, "pattern": PATTERN, "word_bytes": 4})
        self.assertEqual(bytes(target.data), before)

    def test_first_middle_and_last_changed_word_follow_native_boundary(self):
        for offset, expected in ((0, 256), (76, 180), (252, 4)):
            with self.subTest(offset=offset):
                target = StackTarget()
                target.data[offset:offset + 4] = bytes(4)
                self.assertEqual(reader(target).high_water(0x1000, 256)["used_bytes"], expected)

    def test_both_elf_byte_orders(self):
        for byteorder in ("little", "big"):
            target = StackTarget(byteorder)
            target.data[248:252] = (1).to_bytes(4, byteorder)
            self.assertEqual(reader(target).high_water(0x1000, 256)["used_bytes"], 8)

    def test_native_alignment_preserves_unknown_edges(self):
        for architecture, alignment in (("arm", 8), ("xtensa", 16)):
            macros = deepcopy(ARM_MACROS)
            if architecture == "xtensa":
                macros.pop("CONFIG_ARCH_ARM")
                macros.pop("CONFIG_ARCH_ARMV8M")
                macros["CONFIG_ARCH_XTENSA"] = 1
            macros["STACKFRAME_ALIGN"] = alignment
            target = StackTarget()
            target.data[1] = target.data[254] = 0
            result = reader(target, macros).high_water(0x1001, 254)
            self.assertEqual((result["scan_start"], result["scan_size"], result["used_bytes"]),
                             (0x1000 + alignment, 256 - alignment * 2, 0))
            self.assertTrue(all(address >= result["scan_start"] and address + size <= result["scan_start"] + result["scan_size"]
                                for address, size in target.reads))

    def test_disabled_missing_and_unsupported_config_never_becomes_zero(self):
        for name, value in (("CONFIG_STACK_COLORATION", None), ("CONFIG_STACK_COLORATION", 0),
                            ("STACK_COLOR", None), ("STACKFRAME_ALIGN", 4), ("CONFIG_ARCH_ARM", None)):
            target = StackTarget()
            macros = deepcopy(ARM_MACROS)
            if value is None:
                del macros[name]
            else:
                macros[name] = value
            self.assertEqual(reader(target, macros).high_water(0x1000, 256)["status"], "unavailable")
            self.assertEqual(target.reads, [])

    def test_no_aligned_interval_and_bad_word_type_are_unavailable(self):
        target = StackTarget()
        self.assertEqual(reader(target).high_water(0x1001, 4)["status"], "unavailable")
        target.types["uint32_t"] = fixtures.TargetType(size=8)
        self.assertEqual(reader(target).high_water(0x1000, 256)["status"], "unavailable")
        self.assertEqual(target.reads, [])

    def test_limit_is_shared_and_reads_are_bounded(self):
        target = StackTarget()
        target.data = bytearray(PATTERN.to_bytes(4, "little") * 2048)
        instance = reader(target)
        with patch.object(exporter, "MAX_STACK_SCAN_BYTES", 12288):
            self.assertEqual(instance.high_water(0x1000, 8192)["used_bytes"], 0)
            result = instance.high_water(0x1000, 8192)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("per-capture", result["reason"])
        self.assertEqual(instance.read_bytes, 8192)
        self.assertTrue(all(size <= exporter.STACK_READ_BYTES for _, size in target.reads))

    def test_failed_or_short_read_does_not_publish_partial_watermark(self):
        for shortened in (False, True):
            target = StackTarget()
            def bad_read(address, size):
                if shortened:
                    return bytes(size - 1)
                raise RuntimeError("unreadable stack memory")
            target.read_memory = bad_read
            result = reader(target).high_water(0x1000, 256)
            self.assertEqual(result["status"], "unavailable")
            self.assertNotIn("used_bytes", result)

    def test_current_depth_can_exceed_coloration_without_rewriting_evidence(self):
        target = StackTarget()
        target.registers["sp"] = 0x1000
        instance = reader(target)
        self.assertEqual(instance.current(target.task, 0x1000, 256)["sp"], 0x1000)
        self.assertEqual(instance.high_water(0x1000, 256)["used_bytes"], 0)


class CurrentStackTests(unittest.TestCase):
    def test_live_pointer_uses_newest_frame_and_running_identity(self):
        target = StackTarget()
        self.assertEqual(reader(target).current(target.task, 0x1000, 256),
                         {"status": "observed", "sp": 0x10C0, "source": "live_register"})
        self.assertEqual(target.reads, [])

    def test_multiple_or_nonstopped_debugger_threads_are_not_cpu_identity(self):
        for states in ([], [True, True], [False]):
            target = StackTarget()
            target.thread_list = [SimpleNamespace(is_stopped=lambda value=state: value) for state in states]
            result = reader(target).current(target.task, 0x1000, 256)
            self.assertEqual(result["status"], "unavailable")
            self.assertIn("debugger CPU thread", result["reason"])
            self.assertEqual(target.reads, [])

    def test_external_irq_16_is_not_mistaken_for_thread_mode(self):
        for exception in (2, 15, 16, 256, 511):
            target = StackTarget()
            target.registers["xpsr"] |= exception
            result = reader(target).current(target.task, 0x1000, 256)
            self.assertEqual(result["status"], "unavailable")
            self.assertIn("interrupt/exception", result["reason"])

    def test_smp_missing_cpu_config_and_ambiguous_smp_remain_unavailable(self):
        for change in ({"CONFIG_SMP": 1}, {"CONFIG_SMP_NCPUS": 2}, {"CONFIG_SMP_NCPUS": None}):
            target = StackTarget()
            self.assertEqual(reader(target, {**ARM_MACROS, **change}).current(target.task, 0x1000, 256)["status"], "unavailable")
        target = StackTarget()
        instance = reader(target)
        instance.ambiguous = ["CONFIG_SMP"]
        self.assertEqual(instance.current(target.task, 0x1000, 256)["status"], "unavailable")

    def test_wrong_task_state_and_multi_cpu_map_are_unavailable(self):
        target = StackTarget()
        target.values["g_running_tasks"] = fixtures.TargetArray([fixtures.TargetPointer(0x900, {})])
        self.assertIn("does not match", reader(target).current(target.task, 0x1000, 256)["reason"])
        target.values["g_running_tasks"] = fixtures.TargetArray([target.task, target.task])
        self.assertEqual(reader(target).current(target.task, 0x1000, 256)["status"], "unavailable")
        target.values["g_running_tasks"] = fixtures.TargetArray([target.task])
        target.task._fields["task_state"] = 4
        self.assertIn("conflicts", reader(target).current(target.task, 0x1000, 256)["reason"])

    def test_boundary_pointers_are_kept_but_outside_is_unknown(self):
        for sp, expected in ((0x1000, "observed"), (0x1100, "observed"), (0x0FFF, "unavailable"), (0x1101, "unavailable")):
            target = StackTarget()
            target.registers["sp"] = sp
            self.assertEqual(reader(target).current(target.task, 0x1000, 256)["status"], expected)

    def saved_target(self, byteorder="little"):
        target = StackTarget(byteorder)
        target.values["g_running_tasks"] = fixtures.TargetArray([fixtures.TargetPointer(0x900, {})])
        target.task._fields["task_state"] = 4
        target.values["g_tcbinfo"]["reg_off"]["p"][13] = 0
        target.data[128:132] = (0x10D0).to_bytes(4, byteorder)
        return target

    def test_saved_pointer_uses_native_offset_and_elf_byteorder(self):
        for byteorder in ("little", "big"):
            target = self.saved_target(byteorder)
            self.assertEqual(reader(target).current(target.task, 0x1000, 256),
                             {"status": "observed", "sp": 0x10D0, "source": "saved_context"})
            self.assertEqual(target.reads, [(0x1080, 4)])

    def test_missing_register_count_and_sentinel_do_not_read(self):
        for offset, count in ((0xFFFF, 17), (4096, 17), (-4, 17), (3, 17), (0, 13), (0, 257)):
            target = self.saved_target()
            target.values["g_tcbinfo"]["reg_off"]["p"][13] = offset
            target.values["g_tcbinfo"]["regs_num"] = count
            self.assertEqual(reader(target).current(target.task, 0x1000, 256)["status"], "unavailable")
            self.assertEqual(target.reads, [])

    def test_null_misaligned_or_outside_context_is_not_dereferenced(self):
        for address in (0, 0x0FFC, 0x1081, 0x1100):
            target = self.saved_target()
            target.task._fields["xcp"]["regs"] = SavedPointer(address)
            self.assertEqual(reader(target).current(target.task, 0x1000, 256)["status"], "unavailable")
            self.assertEqual(target.reads, [])

    def test_missing_architecture_and_saved_sp_register_fail_closed(self):
        target = self.saved_target()
        target.register_names[13] = "not_sp"
        self.assertEqual(reader(target).current(target.task, 0x1000, 256)["status"], "unavailable")
        self.assertEqual(target.reads, [])
        macros = deepcopy(ARM_MACROS)
        macros.pop("CONFIG_ARCH_ARMV8M")
        self.assertEqual(reader(StackTarget(), macros).current(target.task, 0x1000, 256)["status"], "unavailable")


class StackMacroTests(unittest.TestCase):
    def test_only_unambiguous_literal_values_are_admitted(self):
        constants, ambiguous = exporter.parse_stack_macros([
            " DW_MACRO_define_strp - lineno : 13 macro : CONFIG_ARCH_ARM 1",
            " DW_MACRO_define_strp - lineno : 15 macro : CONFIG_ARCH_ARM 1",
            " DW_MACRO_define - lineno : 22 macro : STACK_COLOR (0xdeadbeefUL)",
            " DW_MACRO_define - lineno : 23 macro : STACKFRAME_ALIGN 8",
            " DW_MACRO_define - lineno : 24 macro : STACKFRAME_ALIGN 16",
            " DW_MACRO_undef - lineno : 25 macro : CONFIG_STACK_COLORATION",
            " DW_MACRO_define - lineno : 26 macro : CONFIG_SMP_NCPUS target_call()",
            " DW_MACRO_define - lineno : 27 macro : unrelated 123",
        ])
        self.assertEqual(constants, {"CONFIG_ARCH_ARM": 1, "STACK_COLOR": PATTERN})
        self.assertEqual(ambiguous, ["CONFIG_SMP_NCPUS", "CONFIG_STACK_COLORATION", "STACKFRAME_ALIGN"])

    def test_c_integer_notation_and_malformed_expressions(self):
        values, ambiguous = exporter.parse_stack_macros([
            " DW_MACRO_define - lineno : 1 macro : STACKFRAME_ALIGN 010U",
            " DW_MACRO_define - lineno : 2 macro : STACK_COLOR ((0xdeadbeefUL))",
            " DW_MACRO_define - lineno : 3 macro : CONFIG_SMP (1",
            " DW_MACRO_define - lineno : 4 macro : CONFIG_SMP_NCPUS 1 + 0",
        ])
        self.assertEqual(values, {"STACKFRAME_ALIGN": 8, "STACK_COLOR": PATTERN})
        self.assertEqual(ambiguous, ["CONFIG_SMP", "CONFIG_SMP_NCPUS"])

    def test_readelf_uses_argv_without_shell_calls(self):
        path = '/tmp/firmware name;$(touch unexpected).elf'
        def invoke(argv, **options):
            self.assertEqual(argv, ["readelf", "--debug-dump=macro", path])
            self.assertNotIn("shell", options)
            options["stdout"].write(b" DW_MACRO_define - lineno : 1 macro : STACK_COLOR 0xdeadbeef\n")
            return SimpleNamespace(returncode=0)
        with patch.object(exporter.subprocess, "run", side_effect=invoke):
            values, ambiguous, error = exporter.read_stack_macros(path)
        self.assertEqual(values, {"STACK_COLOR": PATTERN})
        self.assertEqual(ambiguous, [])
        self.assertIsNone(error)

    def test_tool_timeout_invalid_elf_and_missing_macros_are_unavailable(self):
        for failure in (FileNotFoundError("readelf unavailable"), subprocess.TimeoutExpired("readelf", 15)):
            with patch.object(exporter.subprocess, "run", side_effect=failure):
                values, _, error = exporter.read_stack_macros("/fixture.elf")
            self.assertEqual(values, {})
            self.assertTrue(error)
        for returncode in (0, 1):
            with patch.object(exporter.subprocess, "run", return_value=SimpleNamespace(returncode=returncode)):
                self.assertTrue(exporter.read_stack_macros("/fixture.elf")[2])

    def test_completed_macro_dump_has_an_admission_size_limit(self):
        def invoke(argv, **options):
            options["stdout"].write(b"x" * 33)
            return SimpleNamespace(returncode=0)
        with patch.object(exporter.subprocess, "run", side_effect=invoke), patch.object(exporter, "MAX_MACRO_BYTES", 32):
            values, _, error = exporter.read_stack_macros("/fixture.elf")
        self.assertEqual(values, {})
        self.assertIn("admission limit", error)

    def test_real_elf_dwarf_macros_not_external_build_configuration(self):
        if not shutil.which("readelf"):
            self.skipTest("ELF readelf not installed")
        if compiler := shutil.which("clang"):
            # A host executable is PE on Windows and Mach-O on macOS. Emit an
            # ELF object explicitly, without a host linker, SDK, or runtime.
            command = [compiler, "--target=arm-none-eabi", "-g", "-gdwarf-4", "-fdebug-macro"]
        elif sys.platform.startswith("linux") and (compiler := shutil.which("cc")):
            command = [compiler, "-g3"]
        else:
            self.skipTest("Clang ELF cross compiler or Linux host C compiler not installed")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "stack-evidence.c"
            image = Path(directory) / "stack-evidence.o"
            source.write_text("\n".join(f"#define {name} {value}" for name, value in ARM_MACROS.items()) +
                              "\nint main(void) { return 0; }\n")
            subprocess.run([*command, "-c", str(source), "-o", str(image)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            self.assertEqual(image.read_bytes()[:4], b"\x7fELF", "Macro fixture must use the target ELF format")
            values, ambiguous, error = exporter.read_stack_macros(image)
        self.assertIsNone(error)
        self.assertEqual(values, ARM_MACROS)
        self.assertEqual(ambiguous, [])


class StackSnapshotIntegrationTests(unittest.TestCase):
    def test_observed_fields_keep_heap_accounting_and_capture_provenance(self):
        fixture = fixtures.GdbExporterTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        task = fixture.task()
        task._fields["task_state"] = 3
        fixture.tasks.append(task)
        fixture.gdb.types["uint32_t"] = fixtures.TargetType(size=4)
        original_eval = fixture.gdb.parse_and_eval
        values = {"g_running_tasks": fixtures.TargetArray([task]), "TSTATE_TASK_RUNNING": 3}
        fixture.gdb.parse_and_eval = lambda expression: values[expression] if expression in values else original_eval(expression)
        fixture.gdb.newest_frame = lambda: SimpleNamespace(read_register=lambda name: {"xpsr": 0, "sp": 0x1030}[name])
        fixture.gdb.inferior.read_memory = lambda address, size: PATTERN.to_bytes(4, "little") * (size // 4)
        with patch.object(exporter, "read_stack_macros", return_value=(deepcopy(ARM_MACROS), [], None)):
            document = fixture.capture()
        observation = document["snapshots"][0]["stack_observations"][0]
        self.assertEqual(observation["current"], {"status": "observed", "sp": 0x1030, "source": "live_register"})
        self.assertEqual(observation["high_water"]["used_bytes"], 0)
        self.assertEqual(document["capture"]["tasks"][0]["current_sp"], 0x1030)
        self.assertEqual(document["capture"]["tasks"][0]["high_water_bytes"], 0)
        self.assertFalse(any(span["kind"] == "stack_used" for span in document["snapshots"][0]["spans"]))
        evidence = document["capture"]["stack_evidence"]
        self.assertEqual(evidence["elf_sha256"], document["build"]["elf_sha256"])
        self.assertEqual(evidence["constants"], ARM_MACROS)
        self.assertEqual(evidence["scan_requested_bytes"], 48)


if __name__ == "__main__":
    unittest.main()
