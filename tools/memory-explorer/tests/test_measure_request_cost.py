"""Host admission/parser tests; no ARM toolchain or native execution required."""

import argparse
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

import measure_request_cost as cost


def elf_bytes(items):
    """Independent small ELF32 builder, including nonallocated distractions."""
    names = b"\0"
    sections = [(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)]
    data = bytearray(52)
    for item in [*items, {"name": ".shstrtab", "type": 3}]:
        name = len(names)
        names += item["name"].encode() + b"\0"
        data.extend(b"\0" * (-len(data) % 4))
        start = len(data)
        payload = names if item["name"] == ".shstrtab" else item.get("data", b"")
        size = item.get("size", len(payload))
        if item["type"] != 8:
            data.extend(payload)
        sections.append((name, item["type"], item.get("flags", 0), 0, start, size,
                         item.get("link", 0), 0, 4, item.get("entry_size", 0)))
    data.extend(b"\0" * (-len(data) % 4))
    offset = len(data)
    for section in sections:
        data.extend(struct.pack("<10I", *section))
    data[:52] = struct.pack("<16sHHIIIIIHHHHHH", b"\x7fELF\x01\x01\x01" + b"\0" * 9,
                           1, 40, 1, 0, 0, offset, 0, 52, 0, 0, 40, len(sections), len(sections) - 1)
    return bytes(data)


class CostTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def file(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode() if isinstance(content, str) else content)
        return path

    def context(self, selected="entry"):
        path = "mm/mm_heap/mm_malloc.c"
        self.file(path, selected)
        patches = {name: {"sha256": name, "paths": [path]} for name in ("entry", "paired")}
        ledger = {"upstream_revision": cost.NUTTX_REVISION, "patches": [
            {"name": Path(cost.PATCH).name, "sha256": selected,
             "files": {path: {"before": "original", "after": cost.digest(self.root / path)}}}]}
        return patches, ledger

    def test_baseline_and_candidate_context_ledgers_are_both_admitted(self):
        for selected in ("entry", "paired"):
            with self.subTest(selected=selected):
                patches, ledger = self.context(selected)
                self.assertEqual(cost.admit_context(self.root, ledger, patches)[0], selected)

    def test_wrong_pin_or_patch_cannot_supply_native_context(self):
        patches, ledger = self.context()
        for field, value in (("pin", "wrong"), ("patch", "unrelated")):
            modified = json.loads(json.dumps(ledger))
            if field == "pin":
                modified["upstream_revision"] = value
            else:
                modified["patches"][0]["sha256"] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                cost.admit_context(self.root, modified, patches)

    def test_source_drift_and_incomplete_ledger_are_rejected(self):
        patches, ledger = self.context()
        self.file("mm/mm_heap/mm_malloc.c", "changed")
        with self.assertRaisesRegex(RuntimeError, "source differs"):
            cost.admit_context(self.root, ledger, patches)
        patches, ledger = self.context()
        ledger["patches"][0]["files"] = {}
        with self.assertRaisesRegex(RuntimeError, "inventory"):
            cost.admit_context(self.root, ledger, patches)

    def test_duplicate_patch_ledger_is_rejected(self):
        patches, ledger = self.context()
        ledger["patches"].append(ledger["patches"][0])
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            cost.admit_context(self.root, ledger, patches)

    def test_config_disables_fixture_consistently_and_only_toggles_trace(self):
        source = "CONFIG_EXAMPLES_HEAP_REQUESTS=y\nCONFIG_MM_ALLOC_REQUEST_TRACE=y\nCONFIG_SENTINEL=27\n"
        with mock.patch.object(cost, "validate_configuration"):
            enabled = cost.cost_configuration(source, True)
            disabled = cost.cost_configuration(source, False)
        self.assertIn("# CONFIG_EXAMPLES_HEAP_REQUESTS is not set", enabled)
        self.assertIn("CONFIG_SENTINEL=27", disabled)
        self.assertEqual(enabled.replace("CONFIG_MM_ALLOC_REQUEST_TRACE=y", "# CONFIG_MM_ALLOC_REQUEST_TRACE is not set"), disabled)
        with self.assertRaises(RuntimeError):
            cost.cost_configuration(source, True)

    def test_allocated_sections_are_classified_without_counting_debug_or_relocations(self):
        items = [
            {"name": ".text.fn", "type": 1, "flags": 6, "data": b"X" * 6},
            {"name": ".rodata.s", "type": 1, "flags": 2, "data": b"abc"},
            {"name": ".ARM.exidx.fn", "type": 0x70000001, "flags": 2, "data": b"U" * 8},
            {"name": ".data.v", "type": 1, "flags": 3, "data": b"D" * 4},
            {"name": ".bss.v", "type": 8, "flags": 3, "size": 24},
            {"name": ".debug_info", "type": 1, "data": b"z" * 100},
            {"name": ".rel.text.fn", "type": 9, "data": b"r" * 8},
            {"name": ".unknown", "type": 7, "flags": 2, "data": b"N" * 5},
        ]
        elf = cost.read_elf(self.file("fixture.o", elf_bytes(items)))
        self.assertEqual(elf["totals"], dict(executable=6, readonly=3, unwind=8, writable=4, nobits=24, unsupported=5))
        self.assertEqual(len(elf["allocated_sections"]), 6)
        self.assertEqual(elf["allocated_sections"][-1]["category"], "unsupported")

    def test_nonarm_nonrelocatable_and_truncated_objects_are_rejected(self):
        original = elf_bytes([])
        for offset, value in ((4, 2), (5, 2), (16, 2), (18, 62), (46, 0)):
            data = bytearray(original)
            data[offset] = value
            with self.subTest(offset=offset), self.assertRaises(RuntimeError):
                cost.read_elf(self.file("bad.o", data))
        with self.assertRaises(RuntimeError):
            cost.read_elf(self.file("short.o", original[:51]))

    def test_layout_values_come_from_target_symbol_contents(self):
        values = (4, 256, 8, 16, 16, 56, 40, 56, 40, 32768, 1)
        payload = struct.pack("<11I", *values)
        symbols = bytes(16) + struct.pack("<IIIBBH", 1, 0, len(payload), 1, 0, 1)
        data = elf_bytes([
            {"name": ".rodata.layout", "type": 1, "flags": 2, "data": payload},
            {"name": ".strtab", "type": 3, "data": b"\0nxrs_request_cost_layout\0"},
            {"name": ".symtab", "type": 2, "data": symbols, "link": 2, "entry_size": 16},
        ])
        elf = cost.read_elf(self.file("layout.o", data))
        self.assertEqual(cost.read_layout(elf), dict(zip(cost.LAYOUT_FIELDS, values)))
        elf["symbols"][0]["size"] -= 4
        with self.assertRaisesRegex(RuntimeError, "layout witness"):
            cost.read_layout(elf)

    def test_stack_usage_preserves_dynamic_qualifiers_and_requires_evidence(self):
        path = self.file("unit.su", "src/file.c:3:1:fixed\t16\tstatic\n"
                         "src/file.c:9:1:vla\t32\tdynamic,bounded\n")
        rows = cost.stack_usage(path)
        self.assertEqual([(row["function"], row["bytes"], row["qualifier"]) for row in rows],
                         [("fixed", 16, "static"), ("vla", 32, "dynamic,bounded")])
        for content in ("", "invalid", "x.c:1:1:f\t8\tunknown\n"):
            with self.subTest(content=content), self.assertRaises(RuntimeError):
                cost.stack_usage(self.file("invalid.su", content))
        with self.assertRaises(RuntimeError):
            cost.stack_usage(self.root / "missing.su")

    def test_optimized_gcc_clone_keeps_explicit_stack_symbol_binding(self):
        symbol = {"name": "free_delaylist.isra.0", "type": 2, "section_index": 1}
        rows = [{"function": "free_delaylist.isra", "bytes": 24, "qualifier": "static"}]
        bound = cost.bind_stack_symbols([symbol], rows)
        self.assertEqual(bound[0]["symbol"], "free_delaylist.isra.0")
        self.assertEqual(bound[0]["bytes"], 24)
        self.assertNotIn("symbol", rows[0])

    def test_missing_and_ambiguous_clone_stack_reports_are_rejected(self):
        symbols = [{"name": "function.isra." + str(i), "type": 2, "section_index": 1} for i in range(2)]
        for rows in ([], [{"function": "function", "bytes": 8}], [{"function": "function.isra", "bytes": 8}]):
            with self.subTest(rows=rows), self.assertRaisesRegex(RuntimeError, "ambiguous stack"):
                cost.bind_stack_symbols(symbols, rows)

    def test_compile_disabled_must_have_no_recorder_references(self):
        layout = {"pointer_bytes": 4, "counter_field_bytes": 0}
        self.assertEqual(cost.recorder_storage({"unit": {"recorder_symbols": []}}, layout, "paired", False), 0)
        symbol = {"name": "mm_request_end", "section_index": 0, "size": 0}
        with self.assertRaisesRegex(RuntimeError, "references"):
            cost.recorder_storage({"unit": {"recorder_symbols": [symbol]}}, layout, "paired", False)

    def test_global_id_and_existing_counter_storage_have_separate_admission(self):
        symbols = [{"name": name, "section_index": 1, "size": 8 if name == "g_mm_request_id" else 16}
                   for name in cost.OWNED]
        units = {"unit": {"recorder_symbols": symbols}}
        layout = {"pointer_bytes": 4, "counter_field_bytes": 8}
        self.assertEqual(cost.recorder_storage(units, layout, "paired", True), 8)
        next(s for s in symbols if s["name"] == "g_mm_request_id")["size"] = 16
        with self.assertRaisesRegex(RuntimeError, "global ID"):
            cost.recorder_storage(units, layout, "paired", True)

    def test_comparison_reports_disabled_drift_instead_of_assuming_identity(self):
        def variant(n):
            totals = dict.fromkeys(cost.CATEGORIES, 0) | {"executable": n}
            return {"totals": totals, "layout": {"heap_bytes": 100}, "global_id_bytes": 0,
                    "units": {unit: {"totals": totals} for unit in cost.UNITS}}
        result = cost.compare({"before": variant(10), "after": variant(12)}, "after", "before")
        self.assertEqual(result["allocated_section_bytes"]["executable"], 2)
        self.assertEqual(result["heap_struct_bytes"], 0)

    def test_dependency_paths_with_spaces_and_input_changes(self):
        source = self.file("native source.h", "original")
        depfile = self.file("unit.d", "unit.o: native\\ source.h \\\n native\\ source.h\n")
        self.assertEqual(cost.dependencies(depfile, self.root), [source.resolve()])
        before = cost.hashes([source])
        cost.unchanged(before)
        source.write_text("changed")
        with self.assertRaisesRegex(RuntimeError, "input changed"):
            cost.unchanged(before)

    def test_shared_header_change_between_units_cannot_overwrite_original_receipt(self):
        retained = {"shared/header.h": "first"}
        cost.merge_inputs(retained, {"shared/header.h": "first", "another.h": "stable"})
        with self.assertRaisesRegex(RuntimeError, "between compilation units"):
            cost.merge_inputs(retained, {"shared/header.h": "changed"})
        self.assertEqual(retained["shared/header.h"], "first")

    def test_commands_are_bounded_and_retain_timeout_receipt(self):
        (self.root / "commands").mkdir()
        commands = cost.Commands(self.root)
        with mock.patch.object(cost.subprocess, "run", side_effect=subprocess.TimeoutExpired("cc", 60, output=b"partial")) as run:
            with self.assertRaisesRegex(RuntimeError, "exceeded 60"):
                commands.run(["cc", "unit.c"], self.root)
        self.assertEqual(run.call_args.kwargs["timeout"], 60)
        self.assertEqual((self.root / "commands/000.stdout").read_bytes(), b"partial")
        self.assertTrue(json.loads((self.root / "commands.json").read_text())[0]["timed_out"])

    def test_scratch_patch_applies_inside_an_enclosing_checkout(self):
        subprocess.run(["git", "init", "--quiet", self.root], check=True)
        (self.root / "commands").mkdir()
        native = self.file("target/overlay/unit.c", b"candidate\n")
        patch = self.file("recorder.patch", b"diff --git a/unit.c b/unit.c\n"
                          b"--- a/unit.c\n+++ b/unit.c\n@@ -1 +1 @@\n"
                          b"-original\n+candidate\n")
        commands = cost.Commands(self.root)
        commands.environment.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.autocrlf",
                                     "GIT_CONFIG_VALUE_0": "true"})
        commands.apply_patch(native.parent, patch, reverse=True)
        self.assertEqual(native.read_bytes(), b"original\n")
        commands.apply_patch(native.parent, patch)
        self.assertEqual(native.read_bytes(), b"candidate\n")

    def test_legacy_patch_preserves_context_whitespace_and_rejects_source_changes(self):
        (self.root / "commands").mkdir()
        original = b"  context\n  trailing\n"
        native = self.file("overlay/unit.c", original)
        patch = self.file("recorder.patch", b"diff --git a/unit.c b/unit.c\n"
                          b"--- a/unit.c\n+++ b/unit.c\n@@ -1,2 +1,3 @@\n"
                          b" context\n+added\n trailing\n")
        commands = cost.Commands(self.root)
        with self.assertRaises(RuntimeError):
            commands.apply_patch(native.parent, patch)
        self.assertEqual(native.read_bytes(), original)
        commands.apply_patch(native.parent, patch, ignore_space_change=True)
        self.assertEqual(native.read_bytes(), b"  context\nadded\n  trailing\n")
        commands.apply_patch(native.parent, patch, reverse=True, ignore_space_change=True)
        self.assertEqual(native.read_bytes(), original)
        changed = b"  changed\n  trailing\n"
        native.write_bytes(changed)
        with self.assertRaises(RuntimeError):
            commands.apply_patch(native.parent, patch, ignore_space_change=True)
        self.assertEqual(native.read_bytes(), changed)

    def test_existing_output_or_output_inside_context_is_rejected_before_commands(self):
        args = argparse.Namespace(output=self.root, nuttx_context=self.root / "context")
        with mock.patch.object(cost, "measure") as measure:
            with self.assertRaises(FileExistsError):
                cost.run(args)
            args.output, args.nuttx_context = self.root / "context/results", self.root / "context"
            with self.assertRaisesRegex(RuntimeError, "outside"):
                cost.run(args)
            measure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
