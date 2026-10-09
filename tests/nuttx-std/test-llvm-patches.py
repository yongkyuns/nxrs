"""Offline application/guard tests for the inactive compiler patch candidates.

Minimal old-side patch contexts exercise the applicator, not LLVM correctness.
The exact downloaded upstream blobs were separately application-checked locally.
This host suite remains an applicator test, not a compiler correctness test.
"""

import importlib.util
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("upstream_patches", ROOT / "tools/apply-nuttx-patches.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)
PINS = ROOT / "platform/rust-llvm/upstream.json"
PATCH_DIR = ROOT / "platform/rust-llvm/patches"


def patch_hunks(patch_path):
    lines = patch_path.read_text().splitlines()
    files = []
    path = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("+++ b/"):
            path = line[6:]
            files.append((path, []))
        match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if match:
            if path is None:
                raise ValueError(f"hunk has no destination path: {patch_path}")
            old_start, old_count = int(match[1]), int(match[2] or 1)
            new_count = int(match[4] or 1)
            operations = []
            consumed_old = consumed_new = 0
            index += 1
            while index < len(lines) and (consumed_old < old_count or consumed_new < new_count):
                row = lines[index]
                if row.startswith((" ", "+", "-")):
                    operation = row[0]
                    operations.append((operation, row[1:]))
                    consumed_old += operation != "+"
                    consumed_new += operation != "-"
                elif not row.startswith("\\ No newline"):
                    raise ValueError(f"truncated hunk in {patch_path}: {path}")
                index += 1
            if (consumed_old, consumed_new) != (old_count, new_count):
                raise ValueError(f"incomplete hunk in {patch_path}: {path}")
            files[-1][1].append((old_start, old_count, new_count, operations))
            continue
        index += 1
    return files


def reconstruct_fixture(patch_paths, destination):
    """Rebuild patch preimages while composing each patch's line shifts."""
    states = {}
    for patch_path in patch_paths:
        for name, hunks in patch_hunks(patch_path):
            state = states.setdefault(name, {"lines": [], "base": {}})
            current, base = state["lines"], state["base"]
            patch_delta = 0
            for old_start, old_count, new_count, operations in hunks:
                start = (old_start - 1 if old_count else old_start) + patch_delta
                old_rows = [row for operation, row in operations if operation != "+"]
                if len(old_rows) != old_count or sum(op != "-" for op, _ in operations) != new_count:
                    raise ValueError(f"inconsistent hunk counts in {patch_path}: {name}")

                def placement_score(candidate):
                    conflicts = matches = 0
                    for offset, expected in enumerate(old_rows):
                        position = candidate + offset
                        if position >= len(current):
                            continue
                        token = current[position]
                        if token[0] == "base":
                            previous = base.get(token[1])
                            if previous is None:
                                continue
                            actual = previous
                        else:
                            actual = token[2]
                        if actual == expected:
                            matches += 1
                        else:
                            conflicts += 1
                    return conflicts, matches

                if old_count:
                    # Historical patch headers retain their original source
                    # coordinates. Earlier patches can shift the hunk while
                    # leaving enough known rows to identify git-apply's offset.
                    nominal_conflicts, _ = placement_score(start)
                    if nominal_conflicts:
                        matching_positions = {}
                        for position, token in enumerate(current):
                            actual = base.get(token[1]) if token[0] == "base" else token[2]
                            if actual is not None:
                                matching_positions.setdefault(actual, []).append(position)
                        candidates = {
                            position - offset
                            for offset, expected in enumerate(old_rows)
                            for position in matching_positions.get(expected, ())
                            if position >= offset
                        }
                        compatible = []
                        for candidate in candidates:
                            conflicts, matches = placement_score(candidate)
                            if conflicts == 0 and matches:
                                compatible.append((matches, -abs(candidate - start), candidate))
                        if compatible:
                            start = max(compatible)[2]

                while len(current) < start + old_count:
                    origin = max((token[1] for token in current if token[0] == "base"),
                                 default=-1) + 1
                    current.append(("base", origin, None))
                end = start + old_count

                for position, expected in enumerate(old_rows, start):
                    token = current[position]
                    if token[0] == "base":
                        previous = base.get(token[1])
                        if previous is not None and previous != expected:
                            raise ValueError(f"conflicting original context in {patch_path}: {name}")
                        base[token[1]] = expected
                    elif token[2] != expected:
                        raise ValueError(f"sequential patch context mismatch in {patch_path}: {name}")

                output, cursor = [], start
                for operation, text in operations:
                    if operation == " ":
                        output.append(current[cursor])
                        cursor += 1
                    elif operation == "-":
                        cursor += 1
                    else:
                        output.append(("patch", None, text))
                current[start:end] = output
                patch_delta += new_count - old_count

    destination.mkdir(parents=True, exist_ok=True)
    for name, state in states.items():
        if not state["base"]:  # A new file is created by its patch.
            continue
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        last = max(state["base"])
        path.write_text("\n".join(state["base"].get(i, "") for i in range(last + 1)) + "\n")


class CompilerPatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.patch_paths = [PATCH_DIR / name for name in tool.SERIES["rust-llvm"]]
        reconstruct_fixture(self.patch_paths, self.source)

        self.pins = json.loads(PINS.read_text())
        # Test only guard/application behavior against this minimal fixture;
        # do not weaken the real pinned upstream hashes or change SDK sources.
        source_files = [name for name in self.pins["before_sha256"]
                        if (self.source / name).is_file()]
        self.pins["before_sha256"] = {name: tool.sha256(self.source / name) for name in source_files}
        metadata = self.base / "pins.json"
        metadata.write_text(json.dumps(self.pins))
        override = patch.dict(tool.PINNED_METADATA, {"rust-llvm": metadata})
        override.start()
        self.addCleanup(override.stop)
        self.record = self.base / "patches.json"

    def apply(self, revision=None, proposal_set=None, component="rust-llvm"):
        return tool.apply(self.source, revision or self.pins["revision"], self.record,
                          component, proposal_set)

    def prepare_proposal_fixture(self, proposal_set):
        manifest = json.loads((ROOT / "platform/rust-llvm/proposals/series.json").read_text())
        proposals = [ROOT / "platform/rust-llvm/proposals" / name
                     for name in manifest["sets"][proposal_set]["patches"]]
        reconstruct_fixture(self.patch_paths + proposals, self.source)
        self.pins["before_sha256"] = {
            name: tool.sha256(self.source / name) for name in self.pins["before_sha256"]}
        (self.base / "pins.json").write_text(json.dumps(self.pins))
        return proposals

    def hashes(self):
        return {name: tool.sha256(self.source / name) for name in self.pins["before_sha256"]}

    def test_series_records_draft_status_and_exact_patch_hashes(self):
        ledger = self.apply()
        self.assertEqual(ledger["qualification"], "draft-unqualified")
        self.assertEqual(ledger["rust_revision"], self.pins["rust_revision"])
        self.assertEqual(json.loads(self.record.read_text()), ledger)
        states = dict(self.pins["before_sha256"])
        for entry in ledger["patches"]:
            self.assertEqual(entry["sha256"], tool.sha256(
                ROOT / "platform/rust-llvm/patches" / entry["name"]))
            for name, hashes in entry["files"].items():
                self.assertEqual(hashes["before"], states.get(name))
                states[name] = hashes["after"]
        for name, expected in states.items():
            self.assertEqual(expected, tool.sha256(self.source / name))
        lowering = (self.source / tool.MARKERS["rust-llvm"]).read_text()
        self.assertIn("setOperationAction(ISD::ROTL, MVT::i32, Custom)", lowering)
        self.assertIn("setOperationAction(ISD::ROTR, MVT::i32, Custom)", lowering)
        tti = (self.source / "llvm/lib/Target/Xtensa/XtensaTargetTransformInfo.cpp").read_text()
        self.assertIn('"disable-xtensa-hwloops", cl::Hidden, cl::init(false)', tti)

    def test_wrong_revision_is_rejected_before_mutation(self):
        before = self.hashes()
        with self.assertRaisesRegex(ValueError, "pinned revision"):
            self.apply("different-revision")
        self.assertEqual(before, self.hashes())
        self.assertFalse(self.record.exists())

    def test_unknown_proposal_set_is_rejected_before_mutation(self):
        before = self.hashes()
        with self.assertRaisesRegex(ValueError, "unknown rust-llvm proposal set"):
            self.apply(proposal_set="arbitrary-patch-path")
        self.assertEqual(before, self.hashes())
        self.assertFalse(self.record.exists())

    def test_proposal_set_is_rejected_for_non_llvm_before_mutation(self):
        before = self.hashes()
        with self.assertRaisesRegex(ValueError, "only for rust-llvm"):
            self.apply(proposal_set="alignment", component="nuttx")
        self.assertEqual(before, self.hashes())
        self.assertFalse(self.record.exists())

    def test_named_proposal_sets_are_applied_and_recorded_after_six(self):
        manifest = json.loads((ROOT / "platform/rust-llvm/proposals/series.json").read_text())
        for selection, expected_count in (("alignment", 102), ("setlt", 104)):
            with self.subTest(selection=selection):
                # Each selection starts from the identical six-patch fixture.
                self.source = self.base / f"source-{selection}"
                self.source.mkdir()
                reconstruct_fixture(self.patch_paths, self.source)
                self.pins["before_sha256"] = {
                    name: tool.sha256(self.source / name)
                    for name in self.pins["before_sha256"]}
                (self.base / "pins.json").write_text(json.dumps(self.pins))
                proposals = self.prepare_proposal_fixture(selection)
                initial_states = {
                    path.relative_to(self.source).as_posix(): tool.sha256(path)
                    for path in self.source.rglob("*") if path.is_file()}
                ledger = self.apply(proposal_set=selection)
                proposal_rows = ledger["patches"][6:]
                self.assertEqual([row["name"] for row in proposal_rows],
                                 [path.name for path in proposals])
                self.assertTrue(all(row.get("proposal") is True for row in proposal_rows))
                self.assertEqual(ledger["proposal_set"], selection)
                self.assertEqual(ledger["qualification_tests"], expected_count)
                states = initial_states
                for entry in ledger["patches"]:
                    patch_root = ROOT / "platform/rust-llvm" / (
                        "proposals" if entry.get("proposal") else "patches")
                    self.assertEqual(entry["sha256"], tool.sha256(patch_root / entry["name"]))
                    for name, hashes in entry["files"].items():
                        self.assertEqual(hashes["before"], states.get(name))
                        states[name] = hashes["after"]
                        self.assertRegex(hashes["after"], r"^[0-9a-f]{64}$")
                self.assertEqual(json.loads(self.record.read_text()), ledger)

    def test_wide_iv_extra_preimage_is_checked_before_any_patch(self):
        proposals = self.prepare_proposal_fixture("wide-iv")
        manifest = json.loads((ROOT / "platform/rust-llvm/proposals/series.json").read_text())
        manifest["sets"]["wide-iv"]["before_sha256"][
            "llvm/lib/Analysis/IVUsers.cpp"] = "0" * 64
        manifest_path = self.base / "proposals.json"
        manifest_path.write_text(json.dumps(manifest))
        override = patch.object(tool, "PROPOSAL_MANIFEST", manifest_path)
        override.start()
        self.addCleanup(override.stop)
        before = {path.relative_to(self.source).as_posix(): tool.sha256(path)
                  for path in self.source.rglob("*") if path.is_file()}

        with self.assertRaisesRegex(ValueError, "extra source blob is incompatible"):
            self.apply(proposal_set="wide-iv")

        after = {path.relative_to(self.source).as_posix(): tool.sha256(path)
                 for path in self.source.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertFalse(self.record.exists())
        self.assertEqual(len(proposals), 3)

    def test_wide_iv_ledger_records_extra_qualification_metadata(self):
        self.check_extra_qualification_ledger("wide-iv", 161, 9)

    def test_wide_exit_ledger_records_indvars_qualification_and_tenth_patch(self):
        self.check_extra_qualification_ledger("wide-exit", 399, 10)

    def test_branch_analysis_ledger_records_eleventh_patch_and_its_test(self):
        ledger = self.check_extra_qualification_ledger("branch-analysis", 399, 11)
        self.assertEqual(ledger["qualification_tests"], 105)

    def test_call_frame_ledger_records_metadata_fix_and_both_regressions(self):
        ledger = self.check_extra_qualification_ledger("call-frame", 399, 12)
        self.assertEqual(ledger["qualification_tests"], 107)

    def test_conditional_move_ledger_binds_additional_source_preimages(self):
        ledger = self.check_extra_qualification_ledger("conditional-move", 399, 13)
        self.assertEqual(ledger["qualification_tests"], 108)
        self.assertIn("llvm/lib/Target/Xtensa/XtensaOperators.td",
                      ledger["extra_qualification"]["before_sha256"])

    def test_paired_branch_ledger_retains_the_ordered_parent_and_new_regression(self):
        ledger = self.check_extra_qualification_ledger("paired-branch", 399, 14)
        self.assertEqual(ledger["qualification_tests"], 109)

    def test_fp_select_ledger_retains_the_parent_chain_and_new_fp_fixture(self):
        ledger = self.check_extra_qualification_ledger("fp-select", 399, 15)
        self.assertEqual(ledger["qualification_tests"], 110)
        self.assertEqual(ledger["proposal_set"], "fp-select")
        manifest = json.loads((ROOT / "platform/rust-llvm/proposals/series.json").read_text())
        proposal_name = "0015-Xtensa-use-Boolean-moves-for-FP-integer-selects.patch"
        expected_names = list(tool.SERIES["rust-llvm"]) + (
            manifest["sets"]["paired-branch"]["patches"] + [proposal_name])
        self.assertEqual([entry["name"] for entry in ledger["patches"]], expected_names)

        proposal_path = ROOT / "platform/rust-llvm/proposals" / proposal_name
        last_patch = ledger["patches"][-1]
        self.assertTrue(last_patch["proposal"])
        self.assertEqual(last_patch["sha256"], tool.sha256(proposal_path))
        fixture_name = "llvm/test/CodeGen/Xtensa/fp-integer-select.ll"
        fixture = self.source / fixture_name
        self.assertEqual(last_patch["files"][fixture_name],
                         {"before": None, "after": tool.sha256(fixture)})
        fixture_hunks = dict(patch_hunks(proposal_path))[fixture_name]
        self.assertEqual(len(fixture_hunks), 1)
        old_start, old_count, _, operations = fixture_hunks[0]
        self.assertEqual((old_start, old_count), (0, 0))
        self.assertEqual(fixture.read_text(),
                         "\n".join(text for operation, text in operations
                                   if operation == "+") + "\n")

    def test_constant_hwloop_ledger_binds_two_new_core_preimages_and_sixteenth_patch(self):
        ledger = self.check_extra_qualification_ledger("constant-hwloop", 407, 16)
        self.assertEqual(ledger["qualification_tests"], 111)
        self.assertEqual(ledger["proposal_set"], "constant-hwloop")
        manifest = json.loads((ROOT / "platform/rust-llvm/proposals/series.json").read_text())
        parent = manifest["sets"]["fp-select"]
        proposal_name = "0016-HardwareLoops-admit-fitting-wide-constant-counts.patch"
        self.assertEqual([entry["name"] for entry in ledger["patches"]],
                         list(tool.SERIES["rust-llvm"]) + parent["patches"] + [proposal_name])
        self.assertEqual(set(ledger["extra_qualification"]["before_sha256"]) -
                         set(parent["before_sha256"]),
                         {"llvm/lib/Analysis/TargetTransformInfo.cpp",
                          "llvm/lib/CodeGen/HardwareLoops.cpp"})

    def check_extra_qualification_ledger(self, selection, expected_passes, patch_count,
                                         expected_first_paths=None):
        self.prepare_proposal_fixture(selection)
        manifest = json.loads((ROOT / "platform/rust-llvm/proposals/series.json").read_text())
        entry = manifest["sets"][selection]
        entry["before_sha256"] = {
            path: tool.sha256(self.source / path) for path in entry["before_sha256"]}
        manifest_path = self.base / "proposals.json"
        manifest_path.write_text(json.dumps(manifest))
        self.assertFalse((self.source / "dev/null").exists())
        with patch.object(tool, "PROPOSAL_MANIFEST", manifest_path):
            ledger = self.apply(proposal_set=selection)
        self.assertFalse((self.source / "dev/null").exists())

        extra = ledger["extra_qualification"]
        self.assertEqual(len(ledger["patches"]), patch_count)
        self.assertEqual(extra["before_sha256"], entry["before_sha256"])
        expected_paths = [
            "llvm/test/Analysis/IVUsers",
            "llvm/test/Transforms/LoopStrengthReduce",
        ]
        if selection != "wide-iv":
            expected_paths.append("llvm/test/Transforms/IndVarSimplify")
        if selection in ("constant-hwloop", "hardening", "mixed-mul", "mul-width", "zero-compare", "signed-overflow"):
            expected_paths.append("llvm/test/Transforms/HardwareLoops")
        if expected_first_paths is not None:
            expected_paths = expected_first_paths
        self.assertEqual(extra["test_suites"][0]["paths"], expected_paths)
        self.assertEqual(extra["test_suites"][0]["expected_passes"], expected_passes)
        self.assertEqual(extra["test_suites"][0]["count_scope"], "llvm-lit Passed count")
        self.assertEqual(json.loads(self.record.read_text()), ledger)
        return ledger

    def test_mixed_mul_extends_hardening_and_gates_affected_x86_overflow_tests(self):
        ledger = self.check_extra_qualification_ledger("mixed-mul", 409, 18)
        manifest = json.loads((ROOT / "platform/rust-llvm/proposals/series.json").read_text())
        parent = manifest["sets"]["hardening"]
        selected = manifest["sets"]["mixed-mul"]
        self.assertEqual(selected["patches"], parent["patches"] +
                         ["0018-SelectionDAG-expand-mixed-widening-multiply.patch"])
        self.assertEqual(ledger["qualification_tests"], 114)
        self.assertEqual(selected["extra_test_suites"][0], parent["extra_test_suites"][0])
        self.assertEqual(ledger["extra_qualification"]["test_suites"][1]["expected_passes"], 5)
        changed = set(ledger["patches"][-1]["files"])
        self.assertEqual(changed, {
            "llvm/lib/CodeGen/SelectionDAG/TargetLowering.cpp",
            "llvm/test/CodeGen/X86/xmulo.ll",
            "llvm/test/CodeGen/X86/smulo-128-legalisation-lowering.ll",
            "llvm/test/CodeGen/Xtensa/mixed-widening-multiply.ll",
        })
        self.assertEqual(set(selected["before_sha256"]) - set(parent["before_sha256"]),
                         changed - {"llvm/test/CodeGen/Xtensa/mixed-widening-multiply.ll"})

    def test_mul_width_preserves_parent_chain_and_adds_only_target_experiment(self):
        ledger = self.check_extra_qualification_ledger("mul-width", 409, 19)
        manifest = json.loads((ROOT / "platform/rust-llvm/proposals/series.json").read_text())
        parent, selected = (manifest["sets"][key] for key in ("mixed-mul", "mul-width"))
        self.assertEqual(selected["patches"], parent["patches"] +
                         ["0019-Xtensa-bypass-wide-signed-overflow-multiply.patch"])
        self.assertEqual(ledger["qualification_tests"], 115)
        self.assertEqual(selected["extra_test_suites"], parent["extra_test_suites"])
        self.assertEqual(set(ledger["patches"][-1]["files"]), {
            "llvm/lib/Target/Xtensa/CMakeLists.txt", "llvm/lib/Target/Xtensa/Xtensa.h",
            "llvm/lib/Target/Xtensa/XtensaTargetMachine.cpp",
            "llvm/lib/Target/Xtensa/XtensaMulOverflow.cpp",
            "llvm/test/CodeGen/Xtensa/signed-mul-overflow-bypass.ll"})

    def test_zero_compare_forks_from_mixed_mul_without_runtime_width_pass(self):
        ledger = self.check_extra_qualification_ledger("zero-compare", 409, 19)
        manifest = json.loads((ROOT / "platform/rust-llvm/proposals/series.json").read_text())
        parent, selected = (manifest["sets"][key] for key in ("mixed-mul", "zero-compare"))
        self.assertEqual(selected["patches"], parent["patches"] +
                         ["0020-Xtensa-select-zero-comparisons-directly.patch"])
        self.assertEqual(ledger["qualification_tests"], 115)
        self.assertEqual(selected["extra_test_suites"], parent["extra_test_suites"])
        self.assertEqual(selected["before_sha256"], parent["before_sha256"])
        self.assertEqual(set(ledger["patches"][-1]["files"]), {
            "llvm/lib/Target/Xtensa/XtensaInstrInfo.td",
            "llvm/test/CodeGen/Xtensa/zero-compare.ll"})
        self.assertFalse((self.source / "llvm/lib/Target/Xtensa/XtensaMulOverflow.cpp").exists())

    def test_signed_overflow_preserves_parent_and_binds_generic_preimage(self):
        ledger = self.check_extra_qualification_ledger("signed-overflow", 409, 20)
        manifest = json.loads((ROOT / "platform/rust-llvm/proposals/series.json").read_text())
        parent, selected = (manifest["sets"][key] for key in ("zero-compare", "signed-overflow"))
        self.assertEqual(selected["patches"], parent["patches"] +
                         ["0021-SelectionDAG-check-signed-multiply-with-leading-sign-bits.patch"])
        self.assertEqual(ledger["qualification_tests"], 116)
        self.assertEqual(selected["extra_test_suites"][0], parent["extra_test_suites"][0])
        self.assertEqual(selected["extra_test_suites"][1]["paths"],
                         parent["extra_test_suites"][1]["paths"] +
                         ["llvm/test/CodeGen/X86/signed-overflow-signbits.ll"])
        self.assertEqual(selected["extra_test_suites"][1]["tests"], 6)
        self.assertEqual(set(selected["before_sha256"]) - set(parent["before_sha256"]),
                         {"llvm/lib/CodeGen/SelectionDAG/LegalizeIntegerTypes.cpp"})
        self.assertEqual(set(ledger["patches"][-1]["files"]), {
            "llvm/lib/CodeGen/SelectionDAG/LegalizeIntegerTypes.cpp",
            "llvm/test/CodeGen/Xtensa/signed-overflow-signbits.ll",
            "llvm/test/CodeGen/X86/signed-overflow-signbits.ll"})
        self.assertNotIn("0019-Xtensa-bypass-wide-signed-overflow-multiply.patch", selected["patches"])

    def test_native_sext_appends_one_patch_and_binds_xtensa_preimages(self):
        expected_paths = [
            "llvm/test/Analysis/IVUsers",
            "llvm/test/Transforms/LoopStrengthReduce",
            "llvm/test/Transforms/IndVarSimplify",
            "llvm/test/Transforms/HardwareLoops",
        ]
        ledger = self.check_extra_qualification_ledger(
            "native-sext", 409, 21, expected_first_paths=expected_paths)
        manifest = json.loads((ROOT / "platform/rust-llvm/proposals/series.json").read_text())
        parent, selected = (manifest["sets"][key]
                            for key in ("signed-overflow", "native-sext"))
        proposal_name = "0022-Xtensa-select-native-sign-extension.patch"
        self.assertEqual(selected["patches"], parent["patches"] + [proposal_name])
        self.assertEqual(ledger["qualification_tests"], 117)
        self.assertEqual(selected["extra_test_suites"], parent["extra_test_suites"])
        self.assertEqual(set(selected["before_sha256"]) - set(parent["before_sha256"]),
                         {"llvm/test/CodeGen/Xtensa/load.ll"})
        changed = {
            "llvm/lib/Target/Xtensa/XtensaISelLowering.cpp",
            "llvm/lib/Target/Xtensa/XtensaInstrInfo.td",
            "llvm/test/CodeGen/Xtensa/load.ll",
            "llvm/test/CodeGen/Xtensa/sign-extend-inreg.ll",
        }
        last = ledger["patches"][-1]
        self.assertEqual(set(last["files"]), changed)
        proposal = ROOT / "platform/rust-llvm/proposals" / proposal_name
        self.assertEqual(last["sha256"], tool.sha256(proposal))
        self.assertNotIn("0019-Xtensa-bypass-wide-signed-overflow-multiply.patch",
                         selected["patches"])
        fixture_name = "llvm/test/CodeGen/Xtensa/sign-extend-inreg.ll"
        self.assertIsNone(last["files"][fixture_name]["before"])
        self.assertEqual(last["files"][fixture_name]["after"],
                         tool.sha256(self.source / fixture_name))

    def test_guarded_casts_appends_one_patch_and_binds_codegenprepare_preimage(self):
        expected_paths = [
            "llvm/test/Analysis/IVUsers",
            "llvm/test/Transforms/LoopStrengthReduce",
            "llvm/test/Transforms/IndVarSimplify",
            "llvm/test/Transforms/HardwareLoops",
            "llvm/test/Transforms/CodeGenPrepare",
        ]
        ledger = self.check_extra_qualification_ledger(
            "guarded-casts", 467, 22, expected_first_paths=expected_paths)
        manifest = json.loads((ROOT / "platform/rust-llvm/proposals/series.json").read_text())
        parent, selected = (manifest["sets"][key]
                            for key in ("native-sext", "guarded-casts"))
        proposal_name = "0023-CodeGenPrepare-guard-expanded-saturating-FP-conversion.patch"
        self.assertEqual(selected["patches"], parent["patches"] + [proposal_name])
        self.assertEqual(ledger["qualification_tests"], 118)
        self.assertEqual(selected["extra_test_suites"][1]["paths"],
                         parent["extra_test_suites"][1]["paths"] +
                         ["llvm/test/CodeGen/X86/guarded-fptoint-sat.ll"])
        self.assertEqual(selected["extra_test_suites"][1]["tests"], 7)
        self.assertEqual(set(selected["before_sha256"]) - set(parent["before_sha256"]),
                         {"llvm/lib/CodeGen/CodeGenPrepare.cpp"})
        changed = {
            "llvm/lib/CodeGen/CodeGenPrepare.cpp",
            "llvm/test/CodeGen/Xtensa/guarded-fptoint-sat.ll",
            "llvm/test/CodeGen/X86/guarded-fptoint-sat.ll",
        }
        last = ledger["patches"][-1]
        self.assertEqual(set(last["files"]), changed)
        proposal = ROOT / "platform/rust-llvm/proposals" / proposal_name
        self.assertEqual(last["sha256"], tool.sha256(proposal))
        self.assertNotIn("0019-Xtensa-bypass-wide-signed-overflow-multiply.patch",
                         selected["patches"])
        for fixture_name in changed - {"llvm/lib/CodeGen/CodeGenPrepare.cpp"}:
            self.assertIsNone(last["files"][fixture_name]["before"])
            self.assertEqual(last["files"][fixture_name]["after"],
                             tool.sha256(self.source / fixture_name))

    def test_new_proposal_fixtures_are_absent_from_default_selection(self):
        ledger = self.apply()
        proposal_names = {
            "0022-Xtensa-select-native-sign-extension.patch",
            "0023-CodeGenPrepare-guard-expanded-saturating-FP-conversion.patch",
        }
        self.assertTrue(proposal_names.isdisjoint(
            {entry["name"] for entry in ledger["patches"]}))
        self.assertIsNone(ledger["proposal_set"])
        for path in (
                "llvm/test/CodeGen/Xtensa/sign-extend-inreg.ll",
                "llvm/test/CodeGen/Xtensa/guarded-fptoint-sat.ll",
                "llvm/test/CodeGen/X86/guarded-fptoint-sat.ll"):
            self.assertFalse((self.source / path).exists(), path)

    def test_fpclass_casts_preserves_parent_and_classifies_only_signed_low_result(self):
        manifest = json.loads((ROOT / 'platform/rust-llvm/proposals/series.json').read_text())
        parent, selected = (manifest['sets'][key] for key in ('guarded-casts', 'fpclass-casts'))
        ledger = self.check_extra_qualification_ledger('fpclass-casts', 467, 23,
            expected_first_paths=parent['extra_test_suites'][0]['paths'])
        name = '0024-CodeGenPrepare-classify-NaN-in-guarded-casts.patch'
        self.assertEqual(selected['patches'], parent['patches'] + [name])
        self.assertEqual(selected['before_sha256'], parent['before_sha256'])
        self.assertEqual(selected['extra_test_suites'], parent['extra_test_suites'])
        self.assertEqual(ledger['qualification_tests'], 118)
        self.assertEqual(set(ledger['patches'][-1]['files']), {
            'llvm/lib/CodeGen/CodeGenPrepare.cpp',
            'llvm/test/CodeGen/Xtensa/guarded-fptoint-sat.ll',
            'llvm/test/CodeGen/X86/guarded-fptoint-sat.ll'})
        source = (self.source / 'llvm/lib/CodeGen/CodeGenPrepare.cpp').read_text()
        self.assertIn('B.CreateIntrinsic(Intrinsic::is_fpclass, SrcTy,', source)
        self.assertIn('{Src, B.getInt32(fcNan)}', source)

    def test_soft_casts_preserves_parent_and_checks_promoted_helper_width(self):
        manifest = json.loads((ROOT / 'platform/rust-llvm/proposals/series.json').read_text())
        parent, selected = (manifest['sets'][key] for key in ('fpclass-casts', 'soft-casts'))
        ledger = self.check_extra_qualification_ledger('soft-casts', 467, 24,
            expected_first_paths=parent['extra_test_suites'][0]['paths'])
        name = '0025-CodeGenPrepare-guard-soft-float-saturating-conversions.patch'
        self.assertEqual(selected['patches'], parent['patches'] + [name])
        self.assertEqual(selected['before_sha256'], parent['before_sha256'])
        self.assertEqual(selected['extra_test_suites'], parent['extra_test_suites'])
        self.assertEqual(ledger['qualification_tests'], 119)
        self.assertEqual(set(ledger['patches'][-1]['files']), {
            'llvm/lib/CodeGen/CodeGenPrepare.cpp',
            'llvm/test/CodeGen/Xtensa/soft-fptoint-sat.ll'})
        source = (self.source / 'llvm/lib/CodeGen/CodeGenPrepare.cpp').read_text()
        self.assertIn('TargetLowering::TypeSoftenFloat', source)
        self.assertIn('TargetLowering::TypePromoteInteger', source)
        self.assertIn('RTLIB::getFPTOSINT(SrcVT, ConvertVT)', source)

    def test_zero_select_freezes_newly_unconditional_predicate(self):
        manifest = json.loads((ROOT / 'platform/rust-llvm/proposals/series.json').read_text())
        parent, selected = (manifest['sets'][key] for key in ('bit-branch', 'zero-select'))
        ledger = self.check_extra_qualification_ledger('zero-select', 467, 28,
            expected_first_paths=parent['extra_test_suites'][0]['paths'])
        name = '0029-SelectionDAG-simplify-zero-defaulting-selects.patch'
        self.assertEqual(selected['patches'], parent['patches'] + [name])
        self.assertEqual(selected['before_sha256'], parent['before_sha256'])
        self.assertEqual(ledger['qualification_tests'], 123)
        self.assertEqual(selected['extra_test_suites'][1]['tests'], 18)
        self.assertEqual(set(ledger['patches'][-1]['files']), {
            'llvm/lib/CodeGen/SelectionDAG/DAGCombiner.cpp',
            'llvm/test/CodeGen/Xtensa/zero-select-condition.ll',
            'llvm/test/CodeGen/X86/zero-select-condition.ll'})
        source = (self.source / 'llvm/lib/CodeGen/SelectionDAG/DAGCombiner.cpp').read_text()
        self.assertIn('return DAG.getFreeze(CC == ISD::SETEQ ? InnerFalse : InnerTrue);', source)
        self.assertIn('if (++NumUsers > 4)', source)
        self.assertIn('selectArmsAreZeroWhenZero(LHS, UserTrue, UserFalse)', source)
        fixture = (self.source / 'llvm/test/CodeGen/Xtensa/zero-select-condition.ll').read_text()
        for case in ('shared_flag', 'negative_and', 'negative_nonzero_result',
                     'zero_dynamic_poison', 'zero_dynamic_poison_ne'):
            self.assertIn('@' + case + '(', fixture)

    def test_mul_range_preserves_both_results_and_requires_static_unsigned_fit(self):
        manifest = json.loads((ROOT / 'platform/rust-llvm/proposals/series.json').read_text())
        parent, selected = (manifest['sets'][key] for key in ('zero-select', 'mul-range'))
        ledger = self.check_extra_qualification_ledger('mul-range', 467, 29,
            expected_first_paths=parent['extra_test_suites'][0]['paths'])
        self.assertEqual(selected['patches'], parent['patches'] + [
            '0030-SelectionDAG-simplify-bounded-signed-multiply.patch'])
        self.assertEqual(selected['before_sha256'], parent['before_sha256'])
        self.assertEqual(ledger['qualification_tests'], 124)
        self.assertEqual(selected['extra_test_suites'][1]['tests'], 19)
        self.assertEqual(set(ledger['patches'][-1]['files']), {
            'llvm/lib/CodeGen/SelectionDAG/DAGCombiner.cpp',
            'llvm/test/CodeGen/Xtensa/smulo-known-range.ll',
            'llvm/test/CodeGen/X86/smulo-known-range.ll'})
        source = (self.source / 'llvm/lib/CodeGen/SelectionDAG/DAGCombiner.cpp').read_text()
        self.assertIn('IsSigned && VT.isScalarInteger() && !TLI.isTypeLegal(VT)', source)
        self.assertIn('DAG.SignBitIsZero(N0) && DAG.SignBitIsZero(N1)', source)
        self.assertIn('DAG.willNotOverflowMul(false, N0, N1)', source)
        self.assertIn('return CombineTo(N, Product, Overflow)', source)
        for target, names in (('Xtensa', ('range_32_32', 'range_24_40',
                                         'range_33_32', 'negative_operand')),
                              ('X86', ('range_64_64', 'range_40_88',
                                       'range_65_64', 'legal_32_32'))):
            fixture = (self.source / f'llvm/test/CodeGen/{target}/smulo-known-range.ll').read_text()
            for name in names:
                self.assertIn('@' + name + '(', fixture)

    def test_sign_mask_reuses_existing_extension_only_after_legalization(self):
        manifest = json.loads((ROOT / 'platform/rust-llvm/proposals/series.json').read_text())
        parent, selected = (manifest['sets'][key] for key in ('soft-casts', 'sign-mask'))
        ledger = self.check_extra_qualification_ledger('sign-mask', 467, 25,
            expected_first_paths=parent['extra_test_suites'][0]['paths'])
        name = '0026-Xtensa-reuse-native-extension-for-sign-mask.patch'
        self.assertEqual(selected['patches'], parent['patches'] + [name])
        self.assertEqual(selected['before_sha256'], parent['before_sha256'])
        self.assertEqual(selected['extra_test_suites'], parent['extra_test_suites'])
        self.assertEqual(ledger['qualification_tests'], 120)
        self.assertEqual(set(ledger['patches'][-1]['files']), {
            'llvm/lib/Target/Xtensa/XtensaISelLowering.cpp',
            'llvm/test/CodeGen/Xtensa/reuse-sign-mask.ll'})
        source = (self.source / 'llvm/lib/Target/Xtensa/XtensaISelLowering.cpp').read_text()
        self.assertIn('Subtarget.hasSEXT() && DCI.isAfterLegalizeDAG()', source)
        self.assertIn('DAG.getNodeIfExists(', source)
        self.assertIn('!Shift.hasOneUse()', source)
        self.assertIn('Left->getZExtValue() != 16 && Left->getZExtValue() != 24', source)

    def test_bit_branch_extends_frozen_chain_with_generic_branch_combine_fix(self):
        manifest = json.loads((ROOT / 'platform/rust-llvm/proposals/series.json').read_text())
        parent, selected = (manifest['sets'][key] for key in ('sign-mask', 'bit-branch'))
        ledger = self.check_extra_qualification_ledger('bit-branch', 467, 27,
            expected_first_paths=parent['extra_test_suites'][0]['paths'])
        bit_branch_name = '0027-Xtensa-branch-on-single-bit-tests.patch'
        generic_fix_name = '0028-SelectionDAG-discard-unused-BR_CC-simplifications.patch'
        self.assertEqual(selected['patches'], parent['patches'] + [
            bit_branch_name, generic_fix_name])
        self.assertEqual(selected['extra_test_suites'][0], parent['extra_test_suites'][0])
        self.assertEqual(selected['extra_test_suites'][1]['paths'][:7],
                         parent['extra_test_suites'][1]['paths'])
        self.assertEqual(selected['extra_test_suites'][1]['tests'], 12)
        self.assertEqual(selected['before_sha256'], {
            **parent['before_sha256'],
            'llvm/test/CodeGen/Xtensa/xtensa-icmp.ll':
                'a98c3f9bf8b36f3ad2fe5e0606d274ee27fddbabd24364b1e733137e2a858161',
            'llvm/lib/CodeGen/SelectionDAG/DAGCombiner.cpp':
                'd272bbd3104d0abcb7daefa19a4a377b01190077b3cca4b3d0d467d5572b30d3'})
        self.assertEqual(ledger['qualification_tests'], 122)
        bit_branch = ledger['patches'][-2]
        self.assertEqual(bit_branch['name'], bit_branch_name)
        self.assertEqual(set(bit_branch['files']), {
            'llvm/lib/Target/Xtensa/' + file for file in (
                'XtensaConstantIsland.cpp', 'XtensaISelLowering.cpp',
                'XtensaISelLowering.h', 'XtensaInstrInfo.cpp',
                'XtensaInstrInfo.td', 'XtensaOperators.td')
        } | {'llvm/test/CodeGen/Xtensa/' + file for file in (
            'bit-test-branch.ll', 'bit-test-branch.mir', 'xtensa-icmp.ll')})
        source = (self.source / 'llvm/lib/Target/Xtensa/XtensaISelLowering.cpp').read_text()
        self.assertIn('!LHS.hasOneUse()', source)
        self.assertIn('!Mask->getAPIntValue().isPowerOf2()', source)
        self.assertIn('DAG.getConstant(Mask->getAPIntValue().logBase2()', source)
        for file in ('XtensaInstrInfo.cpp', 'XtensaConstantIsland.cpp'):
            source = (self.source / 'llvm/lib/Target/Xtensa' / file).read_text()
            self.assertIn('case Xtensa::BBCI:', source)
            self.assertIn('case Xtensa::BBSI:', source)

        generic_fix = ledger['patches'][-1]
        self.assertEqual(generic_fix['name'], generic_fix_name)
        self.assertEqual(set(generic_fix['files']), {
            'llvm/lib/CodeGen/SelectionDAG/DAGCombiner.cpp'})
        dag_combiner = (self.source / 'llvm/lib/CodeGen/SelectionDAG/DAGCombiner.cpp').read_text()
        self.assertIn('recursivelyDeleteUnusedNodes(Simp.getNode());', dag_combiner)

    def test_hardening_extends_frozen_chain_with_only_four_new_tests(self):
        ledger = self.check_extra_qualification_ledger("hardening", 409, 17)
        manifest = json.loads((ROOT / "platform/rust-llvm/proposals/series.json").read_text())
        parent = manifest["sets"]["constant-hwloop"]
        selected = manifest["sets"]["hardening"]
        proposal_name = "0017-Tests-harden-predicates-and-wide-loop-boundaries.patch"
        self.assertEqual(selected["patches"], parent["patches"] + [proposal_name])
        self.assertEqual(selected["before_sha256"], parent["before_sha256"])
        self.assertEqual(selected["extra_test_suites"][0]["paths"],
                         parent["extra_test_suites"][0]["paths"])
        self.assertEqual(ledger["qualification_tests"], 113)
        expected = {
            "llvm/test/CodeGen/Xtensa/carry-borrow-predicates.ll",
            "llvm/test/CodeGen/Xtensa/fp-predicate-liveness.ll",
            "llvm/test/Transforms/IndVarSimplify/wide-counter-live-out.ll",
            "llvm/test/Transforms/HardwareLoops/wide-count-32bit-edges.ll",
        }
        last = ledger["patches"][-1]
        self.assertEqual(set(last["files"]), expected)
        evidence_path = ROOT / "tests/arithmetic-parity/results/compiler-hardening-2026-10-06.json"
        evidence = json.loads(evidence_path.read_text())
        self.assertEqual(last["files"], evidence["tests_only_new_files"])
        proposal = ROOT / "platform/rust-llvm/proposals" / proposal_name
        self.assertEqual(last["sha256"], tool.sha256(proposal))
        self.assertEqual(proposal.read_text().count("new file mode 100644"), 4)
        for name, hunks in patch_hunks(proposal):
            self.assertEqual(len(hunks), 1)
            old_start, old_count, _, operations = hunks[0]
            self.assertEqual((old_start, old_count), (0, 0))
            self.assertTrue(all(op == "+" for op, _ in operations))
            fixture = self.source / name
            self.assertEqual(fixture.read_text(),
                             "\n".join(text for _, text in operations) + "\n")
            self.assertEqual(last["files"][name],
                             {"before": None, "after": tool.sha256(fixture)})

    def test_hardening_fixtures_are_absent_from_default_and_sixteen_patch_selections(self):
        proposal = ROOT / "platform/rust-llvm/proposals/0017-Tests-harden-predicates-and-wide-loop-boundaries.patch"
        paths = [name for name, _ in patch_hunks(proposal)]
        self.apply()
        self.assertTrue(all(not (self.source / name).exists() for name in paths))
        self.source = self.base / "sixteen-patch-source"
        self.source.mkdir()
        self.record = self.base / "sixteen-patches.json"
        self.check_extra_qualification_ledger("constant-hwloop", 407, 16)
        self.assertTrue(all(not (self.source / name).exists() for name in paths))

    def test_extra_suite_paths_and_counts_are_validated(self):
        manifest = json.loads((ROOT / "platform/rust-llvm/proposals/series.json").read_text())
        entry = manifest["sets"]["wide-iv"]
        for invalid_path in ("../../outside", "/tmp/test.ll", "llvm/test/../outside"):
            with self.subTest(path=invalid_path):
                candidate = json.loads(json.dumps(manifest))
                candidate["sets"]["wide-iv"]["extra_test_suites"][0]["paths"][0] = invalid_path
                path = self.base / "invalid-proposals.json"
                path.write_text(json.dumps(candidate))
                with patch.object(tool, "PROPOSAL_MANIFEST", path):
                    with self.assertRaises(ValueError):
                        tool.proposal_selection("rust-llvm", "wide-iv", 99)

        for invalid_count in (0, -1, True, "5"):
            with self.subTest(count=invalid_count):
                candidate = json.loads(json.dumps(manifest))
                candidate["sets"]["wide-iv"]["extra_test_suites"][0]["tests"] = invalid_count
                path = self.base / "invalid-proposals.json"
                path.write_text(json.dumps(candidate))
                with patch.object(tool, "PROPOSAL_MANIFEST", path):
                    with self.assertRaisesRegex(ValueError, "extra test count"):
                        tool.proposal_selection("rust-llvm", "wide-iv", 99)
        self.assertEqual(entry["extra_test_suites"][0]["tests"], 161)

    def test_changed_blob_is_rejected_before_mutation(self):
        witness = self.source / tool.MARKERS["rust-llvm"]
        witness.write_text(witness.read_text() + "// different input\n")
        before = self.hashes()
        with self.assertRaisesRegex(ValueError, "source blob is incompatible"):
            self.apply()
        self.assertEqual(before, self.hashes())
        self.assertFalse(self.record.exists())

    def test_reapplication_is_rejected_without_further_changes(self):
        self.apply()
        before = self.hashes()
        with self.assertRaisesRegex(ValueError, "already applied"):
            self.apply()
        self.assertEqual(before, self.hashes())

    def test_git_checkout_is_rejected(self):
        (self.source / ".git").mkdir()
        before = self.hashes()
        with self.assertRaisesRegex(ValueError, "Git checkout"):
            self.apply()
        self.assertEqual(before, self.hashes())
        self.assertFalse(self.record.exists())

    def test_fixture_composes_line_shifts_across_patches(self):
        first = self.base / "first.patch"
        second = self.base / "second.patch"
        first.write_text("""diff --git a/sample.txt b/sample.txt
--- a/sample.txt
+++ b/sample.txt
@@ -1,3 +1,4 @@
 line1
 target
+inserted
 line3
""")
        second.write_text("""diff --git a/sample.txt b/sample.txt
--- a/sample.txt
+++ b/sample.txt
@@ -3,3 +4,3 @@
 inserted
-line3
+finished
 line4
""")
        fixture = self.base / "shift-fixture"
        reconstruct_fixture([first, second], fixture)
        self.assertEqual((fixture / "sample.txt").read_text(),
                         "line1\ntarget\nline3\nline4\n")
        env = tool.git_env(fixture)
        subprocess.run(["git", "apply", str(first)], cwd=fixture, env=env, check=True)
        subprocess.run(["git", "apply", str(second)], cwd=fixture, env=env, check=True)
        self.assertEqual((fixture / "sample.txt").read_text(),
                         "line1\ntarget\ninserted\nfinished\nline4\n")

    def test_fixture_offsets_historical_hunk_to_matching_known_context(self):
        first = self.base / "offset-first.patch"
        second = self.base / "offset-second.patch"
        first.write_text("""diff --git a/sample.txt b/sample.txt
--- a/sample.txt
+++ b/sample.txt
@@ -1,3 +1,5 @@
 head
+insert-a
+insert-b
 before
 target
""")
        second.write_text("""diff --git a/sample.txt b/sample.txt
--- a/sample.txt
+++ b/sample.txt
@@ -3,3 +3,3 @@
 target
-replace-me
+replaced
 tail
""")
        fixture = self.base / "offset-fixture"
        reconstruct_fixture([first, second], fixture)
        self.assertEqual((fixture / "sample.txt").read_text(),
                         "head\nbefore\ntarget\nreplace-me\ntail\n")

        env = tool.git_env(fixture)
        subprocess.run(["git", "apply", str(first)], cwd=fixture, env=env, check=True)
        subprocess.run(["git", "apply", str(second)], cwd=fixture, env=env, check=True)
        self.assertEqual((fixture / "sample.txt").read_text(),
                         "head\ninsert-a\ninsert-b\nbefore\ntarget\nreplaced\ntail\n")

    def test_alignment_proposal_applies_after_six_without_activation(self):
        proposal = ROOT / "platform/rust-llvm/proposals/0007-Xtensa-align-hardware-loops-after-layout.patch"
        self.assertNotIn(proposal.name, tool.SERIES["rust-llvm"])
        reconstruct_fixture(self.patch_paths + [proposal], self.source)
        # The extended preimage includes more real context than setUp's minimal
        # fixture. Refresh only its temporary hashes, never the pinned metadata.
        self.pins["before_sha256"] = {
            name: tool.sha256(self.source / name) for name in self.pins["before_sha256"]}
        (self.base / "pins.json").write_text(json.dumps(self.pins))
        ledger = self.apply()
        self.assertEqual(len(ledger["patches"]), 6)
        self.assertEqual(self.pins["qualification_tests"]["total"], 99)
        env = tool.git_env(self.source)
        subprocess.run(["git", "apply", "--check", str(proposal)], cwd=self.source, env=env, check=True)
        subprocess.run(["git", "apply", str(proposal)], cwd=self.source, env=env, check=True)
        subprocess.run(["git", "apply", "--reverse", "--check", str(proposal)],
                       cwd=self.source, env=env, check=True)
        checker = self.source / "llvm/test/CodeGen/Xtensa/Inputs/hwloop-layout-matrix.py"
        result = subprocess.run(["python3", str(checker), "--self-test"],
                                capture_output=True, text=True, check=True)
        self.assertIn("CHECKER_SELF_TEST_PASS negative_cases=6", result.stdout)
        fixture = self.source / "llvm/test/CodeGen/Xtensa/hwloop-final-alignment.mir"
        generated = subprocess.run(["python3", str(checker), str(fixture)],
                                   capture_output=True, text=True, check=True).stdout
        self.assertEqual(generated.count("    LOOP $a4,"), 12)
        self.assertNotIn("MOVI $a11,", generated)
        self.assertIn("    successors: %bb.3, %bb.4", generated)


if __name__ == "__main__":
    unittest.main()
