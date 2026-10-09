"""Safety and sequencing checks for the opt-in pinned compiler builder."""

import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "build_rust_llvm", ROOT / "tools/build-rust-llvm.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


class RustLlvmBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.llvm = self.repository("llvm", "pinned-llvm")
        self.rust = self.repository("rust", "pinned-rust")
        self.std = self.base / "std-snapshot"
        self.std.mkdir()
        core = self.std / "core/src"
        core.mkdir(parents=True)
        (core / "lib.rs").write_text("// independent core snapshot\n")
        self.output = self.base / "new-evaluation-output"
        self.pins = {"revision": self.head(self.llvm),
                     "rust_revision": self.head(self.rust), "status": "draft-unqualified",
                     "qualification_tests": {"total": 99}}
        self.pin_patch = patch.object(tool, "PINS", self.pins)
        self.pin_patch.start()
        self.addCleanup(self.pin_patch.stop)

    def git(self, cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                              text=True, capture_output=True).stdout.strip()

    def repository(self, name, content):
        repo = self.base / name
        repo.mkdir()
        self.git(repo, "init", "-q")
        (repo / "source.txt").write_text(content + "\n")
        self.git(repo, "add", "source.txt")
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
                       check=True)
        return repo

    def head(self, repo):
        return self.git(repo, "rev-parse", "HEAD")

    def args(self, **updates):
        values = dict(llvm_source=self.llvm, rust_source=self.rust,
                      std_source=self.std, out=self.output, jobs=1, plan=True)
        values.update(updates)
        return type("Args", (), values)()

    def std_provenance_fixture(self):
        proposal_dir = self.base / "std-proposals"
        proposal_dir.mkdir()
        patch_name = "fixture.patch"
        paths = {
            "std/src/num/f32.rs": b"patched f32\n",
            "std/src/num/f64.rs": b"patched f64\n",
            "std/src/sys/cmath.rs": b"patched cmath\n",
        }
        patch_repo = self.base / "patch-source"
        patch_repo.mkdir()
        self.git(patch_repo, "init", "-q")
        before = {}
        for relative, contents in paths.items():
            original = b"before " + relative.encode()
            before[relative] = hashlib.sha256(original).hexdigest()
            target = patch_repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(original)
        self.git(patch_repo, "add", "std")
        subprocess.run(["git", "-C", str(patch_repo), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "before"],
                       check=True)
        for relative, contents in paths.items():
            (patch_repo / relative).write_bytes(contents)
        patch_bytes = subprocess.run(
            ["git", "-C", str(patch_repo), "diff", "--binary", "HEAD"],
            check=True, capture_output=True).stdout
        (proposal_dir / patch_name).write_bytes(patch_bytes)
        for relative, contents in paths.items():
            target = self.std / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(contents)
        manifest = {
            "schema": 1,
            "proposals": {
                "fixture-rfc": {
                    "state": "rfc",
                    "qualification": "unqualified",
                    "selection_scope": "evaluation-preparation-only",
                    "source_revision": self.pins["rust_revision"],
                    "target_os": "nuttx",
                    "patch": {"file": patch_name,
                              "sha256": hashlib.sha256(patch_bytes).hexdigest()},
                    "before_sha256": before,
                }
            },
        }
        manifest_path = proposal_dir / "series.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True))
        std_proposal_tool = tool._std_proposal_module()
        manifest_patch = patch.multiple(
            std_proposal_tool,
            PROPOSAL_DIR=proposal_dir,
            PROPOSAL_MANIFEST=manifest_path)
        manifest_patch.start()
        self.addCleanup(manifest_patch.stop)
        selected = std_proposal_tool._load_proposal("fixture-rfc")
        ledger = {
            "schema": 1,
            "proposal": "fixture-rfc",
            "proposal_state": "rfc",
            "qualification": "unqualified",
            "selection_scope": "evaluation-preparation-only",
            "target_os": "nuttx",
            "source_revision": self.pins["rust_revision"],
            "snapshot_archive_sha256": "a" * 64,
            "proposal_manifest_sha256": selected["manifest_sha256"],
            "patch": {"file": patch_name, "sha256": selected["patch_sha256"]},
            "files": {
                relative: {
                    "before_sha256": before[relative],
                    "after_sha256": hashlib.sha256(contents).hexdigest(),
                }
                for relative, contents in paths.items()
            },
        }
        ledger_path = self.base / "std-provenance.json"
        ledger_path.write_text(json.dumps(ledger))
        return ledger, ledger_path, proposal_dir, manifest_path

    def planned_runner(self):
        runner = Mock()
        runner.plan = True
        return runner

    def test_plan_sequences_pinned_exports_patch_tests_then_stage1(self):
        runner = self.planned_runner()
        with patch("builtins.print"):
            tool.build(self.args(), runner)
        commands = [call.args[0] for call in runner.call_args_list]
        self.assertEqual(commands[0][:5], ["git", "-C", str(self.llvm.resolve()),
                                           "archive", "--format=tar"])
        self.assertEqual(commands[2][:5], ["git", "-C", str(self.rust.resolve()),
                                           "archive", "--format=tar"])
        patch_index = next(i for i, command in enumerate(commands)
                           if str(tool.PATCH_TOOL) in list(map(str, command)))
        cmake_index = next(i for i, command in enumerate(commands)
                           if command[0] == "cmake" and "-S" in command)
        lit_index = next(i for i, command in enumerate(commands)
                         if "llvm-lit" in str(command[0]))
        rust_index = next(i for i, command in enumerate(commands)
                          if "x.py" in command)
        self.assertLess(patch_index, cmake_index)
        self.assertLess(cmake_index, lit_index)
        self.assertLess(lit_index, rust_index)
        self.assertIn("-DLLVM_TARGETS_TO_BUILD=X86", commands[cmake_index])
        self.assertIn("-DLLVM_EXPERIMENTAL_TARGETS_TO_BUILD=Xtensa", commands[cmake_index])
        self.assertIn("-DLLVM_ENABLE_ASSERTIONS=ON", commands[cmake_index])
        self.assertIn("-DLLVM_LINK_LLVM_DYLIB=OFF", commands[cmake_index])
        self.assertIn("--stage", commands[rust_index])
        self.assertFalse(any("--proposal-set" in list(map(str, command)) for command in commands))
        self.assertFalse(self.output.exists())
        self.assertEqual(self.git(self.llvm, "status", "--porcelain"), "")
        self.assertEqual(self.git(self.rust, "status", "--porcelain"), "")

    def test_named_proposal_plan_passes_selection_before_lit_then_rust(self):
        runner = self.planned_runner()
        args = self.args(proposal_set="alignment")
        with patch("builtins.print"):
            tool.build(args, runner)
        commands = [call.args[0] for call in runner.call_args_list]
        patch_index = next(i for i, command in enumerate(commands)
                           if str(tool.PATCH_TOOL) in list(map(str, command)))
        lit_index = next(i for i, command in enumerate(commands)
                         if "llvm-lit" in str(command[0]))
        rust_index = next(i for i, command in enumerate(commands)
                          if "x.py" in command)
        self.assertIn("--proposal-set", commands[patch_index])
        self.assertEqual(commands[patch_index][commands[patch_index].index("--proposal-set") + 1],
                         "alignment")
        self.assertLess(patch_index, lit_index)
        self.assertLess(lit_index, rust_index)
        expected_patch_count, expected_tests = tool.PATCH_MODULE.proposal_selection(
            "rust-llvm", "alignment", 99)
        self.assertEqual(len(expected_patch_count), 1)
        self.assertEqual(expected_tests, 102)
        self.assertFalse(self.output.exists())

    def test_wide_iv_plan_runs_extra_lit_suite_before_rust_stage1(self):
        self.check_extra_suite_plan("wide-iv", 2, 161)

    def test_wide_exit_plan_runs_indvars_before_rust_stage1(self):
        self.check_extra_suite_plan("wide-exit", 3, 399)

    def test_branch_analysis_plan_requires_its_new_xtensa_regression(self):
        self.check_extra_suite_plan("branch-analysis", 3, 399, xtensa_passes=105)

    def test_new_target_proposals_require_their_exact_backend_gates(self):
        for selection, passes in (("call-frame", 107), ("conditional-move", 108),
                                  ("paired-branch", 109), ("fp-select", 110)):
            with self.subTest(selection=selection):
                self.check_extra_suite_plan(selection, 3, 399, xtensa_passes=passes)

    def test_constant_hwloop_plan_runs_four_optimizer_paths_before_rust_stage1(self):
        self.check_extra_suite_plan("constant-hwloop", 4, 407, xtensa_passes=111)

    def test_hardening_plan_requires_tests_only_extension_before_rust_stage1(self):
        self.check_extra_suite_plan("hardening", 4, 409, xtensa_passes=113)

    def test_mixed_mul_plan_runs_optimizer_and_x86_gates_before_rust_stage1(self):
        self.check_extra_suite_plan("mixed-mul", 4, 409, xtensa_passes=114,
                                    additional_paths=5)

    def test_mul_width_plan_keeps_all_parent_gates_before_rust_stage1(self):
        self.check_extra_suite_plan("mul-width", 4, 409, xtensa_passes=115,
                                   additional_paths=5)

    def test_zero_compare_plan_keeps_optimizer_and_x86_gates_before_rust_stage1(self):
        self.check_extra_suite_plan("zero-compare", 4, 409, xtensa_passes=115,
                                   additional_paths=5)

    def test_signed_overflow_plan_gates_both_architectures_before_rust_stage1(self):
        self.check_extra_suite_plan("signed-overflow", 4, 409, xtensa_passes=116,
                                   additional_paths=6)

    def test_native_sext_plan_preserves_parent_gates_before_rust_stage1(self):
        self.check_extra_suite_plan(
            "native-sext", 4, 409, xtensa_passes=117, additional_paths=6,
            expected_first_paths=[
                "llvm/test/Analysis/IVUsers",
                "llvm/test/Transforms/LoopStrengthReduce",
                "llvm/test/Transforms/IndVarSimplify",
                "llvm/test/Transforms/HardwareLoops",
            ])

    def test_guarded_casts_plan_adds_codegenprepare_and_x86_gates(self):
        self.check_extra_suite_plan(
            "guarded-casts", 5, 467, xtensa_passes=118, additional_paths=7,
            expected_first_paths=[
                "llvm/test/Analysis/IVUsers",
                "llvm/test/Transforms/LoopStrengthReduce",
                "llvm/test/Transforms/IndVarSimplify",
                "llvm/test/Transforms/HardwareLoops",
                "llvm/test/Transforms/CodeGenPrepare",
            ])

    def test_fpclass_casts_plan_preserves_the_guarded_cast_gates(self):
        self.check_extra_suite_plan('fpclass-casts', 5, 467, xtensa_passes=118,
                                   additional_paths=7)

    def test_soft_casts_plan_adds_promoted_width_backend_coverage(self):
        self.check_extra_suite_plan('soft-casts', 5, 467, xtensa_passes=119,
                                   additional_paths=7)

    def test_sign_mask_plan_preserves_generic_gates_and_adds_xtensa_coverage(self):
        self.check_extra_suite_plan('sign-mask', 5, 467, xtensa_passes=120,
                                   additional_paths=7)

    def test_bit_branch_plan_keeps_parent_gates_and_adds_branch_coverage(self):
        self.check_extra_suite_plan('bit-branch', 5, 467, xtensa_passes=122,
                                   additional_paths=12)
        manifest = json.loads((ROOT / 'upstream/rust-llvm/proposals/series.json').read_text())
        parent, selected = (manifest['sets'][key] for key in ('sign-mask', 'bit-branch'))
        parent_x86_paths = parent['extra_test_suites'][1]['paths']
        selected_x86_paths = selected['extra_test_suites'][1]['paths']
        self.assertEqual(len(parent_x86_paths), 7)
        self.assertEqual(selected_x86_paths[:7], parent_x86_paths)
        self.assertEqual(selected_x86_paths[7:], [
            'llvm/test/CodeGen/X86/brcc.ll',
            'llvm/test/CodeGen/X86/brcond.ll',
            'llvm/test/CodeGen/X86/isel-brcond-icmp.ll',
            'llvm/test/CodeGen/X86/isel-brcond-fcmp.ll',
            'llvm/test/CodeGen/X86/or-branch.ll',
        ])

    def test_zero_select_plan_keeps_all_gates_before_rust_stage1(self):
        self.check_extra_suite_plan('zero-select', 5, 467, xtensa_passes=123,
                                   additional_paths=18)
        manifest = json.loads((ROOT / 'upstream/rust-llvm/proposals/series.json').read_text())
        parent, selected = (manifest['sets'][key] for key in ('bit-branch', 'zero-select'))
        self.assertEqual(selected['extra_test_suites'][0], parent['extra_test_suites'][0])
        self.assertEqual(selected['extra_test_suites'][1]['paths'][:12],
                         parent['extra_test_suites'][1]['paths'])

    def test_mul_range_plan_preserves_parent_suites_and_adds_wide_x86_control(self):
        self.check_extra_suite_plan('mul-range', 5, 467, xtensa_passes=124,
                                   additional_paths=19)
        manifest = json.loads((ROOT / 'upstream/rust-llvm/proposals/series.json').read_text())
        parent, selected = (manifest['sets'][key] for key in ('zero-select', 'mul-range'))
        self.assertEqual(selected['extra_test_suites'][0], parent['extra_test_suites'][0])
        self.assertEqual(selected['extra_test_suites'][1]['paths'][:-1],
                         parent['extra_test_suites'][1]['paths'])

    def check_extra_suite_plan(self, selection, paths, passes, xtensa_passes=104,
                               additional_paths=0, expected_first_paths=None):
        runner = self.planned_runner()
        with patch("builtins.print"):
            tool.build(self.args(proposal_set=selection), runner)
        commands = [call.args[0] for call in runner.call_args_list]
        lit_commands = [command for command in commands if "llvm-lit" in str(command[0])]
        rust_index = next(i for i, command in enumerate(commands)
                          if "x.py" in command)
        self.assertEqual(len(lit_commands), 3 if additional_paths else 2)
        self.assertLess(commands.index(lit_commands[0]), commands.index(lit_commands[1]))
        self.assertLess(commands.index(lit_commands[1]), rust_index)
        if additional_paths:
            self.assertLess(commands.index(lit_commands[1]), commands.index(lit_commands[2]))
            self.assertLess(commands.index(lit_commands[2]), rust_index)
            self.assertEqual(len(lit_commands[2]) - 2, additional_paths)
        self.assertEqual(len(lit_commands[1]) - 2, paths)
        expected_llvm = self.output.resolve() / "llvm-source"
        self.assertTrue(all(str(path).startswith(str(expected_llvm))
                            for path in lit_commands[1][2:]))
        suite_metadata = tool.PATCH_MODULE.proposal_metadata(
            "rust-llvm", selection, 99)["extra_test_suites"]
        if expected_first_paths is not None:
            self.assertEqual(list(suite_metadata[0]["paths"]), expected_first_paths)
            self.assertEqual(
                [str(path)[len(str(expected_llvm)) + 1:] for path in lit_commands[1][2:]],
                expected_first_paths)
        self.assertEqual(tool.PATCH_MODULE.proposal_selection("rust-llvm", selection, 99)[1],
                         xtensa_passes)
        self.assertEqual(suite_metadata[0]["tests"], passes)
        self.assertFalse(self.output.exists())

    def test_unknown_proposal_fails_before_output_or_commands(self):
        runner = self.planned_runner()
        with self.assertRaisesRegex(ValueError, "unknown rust-llvm proposal set"):
            tool.build(self.args(proposal_set="arbitrary.patch"), runner)
        self.assertEqual(runner.call_count, 0)
        self.assertFalse(self.output.exists())

    def test_lit_gate_requires_exact_pinned_count(self):
        runner = tool.CommandRunner()
        command = ["llvm-lit", "-sv"]
        passed = subprocess.CompletedProcess(command, 0, "  Passed: 99 (100.00%)\n", "")
        with patch.object(tool.subprocess, "run", return_value=passed), patch("builtins.print"):
            runner.run_lit(command, 99)
        failed_count = subprocess.CompletedProcess(command, 0, "Passed: 97 (1.2s)\n", "")
        with patch.object(tool.subprocess, "run", return_value=failed_count), patch("builtins.print"):
            with self.assertRaisesRegex(ValueError, "expected all 99"):
                runner.run_lit(command, 99)

    def test_lit_gate_accepts_exact_alignment_count(self):
        runner = tool.CommandRunner()
        command = ["llvm-lit", "-sv"]
        passed = subprocess.CompletedProcess(command, 0, "Passed: 102 (100.00%)\n", "")
        with patch.object(tool.subprocess, "run", return_value=passed), patch("builtins.print"):
            runner.run_lit(command, 102)
        failed_count = subprocess.CompletedProcess(command, 0, "Passed: 99 (1.2s)\n", "")
        with patch.object(tool.subprocess, "run", return_value=failed_count), patch("builtins.print"):
            with self.assertRaisesRegex(ValueError, "expected all 102"):
                runner.run_lit(command, 102)

    def test_extra_lit_gate_requires_supported_pass_count_and_records_discovery(self):
        runner = tool.CommandRunner()
        command = ["llvm-lit", "-sv", "llvm/test/Analysis/IVUsers",
                   "llvm/test/Transforms/LoopStrengthReduce"]
        summary = ("Passed: 161 (100.00%)\nUnsupported: 36\n"
                   "Expectedly Failed: 1\n")
        passed = subprocess.CompletedProcess(command, 0, summary, "")
        with patch.object(tool.subprocess, "run", return_value=passed), patch("builtins.print"):
            counts = runner.run_lit(command, 161, suite_label="extra LLVM lit suite")
        self.assertEqual(counts["passed"], 161)
        self.assertEqual(counts["unsupported"], 36)
        self.assertEqual(counts["expectedly_failed"], 1)
        self.assertEqual(counts["failed"], 0)

        failed_count = subprocess.CompletedProcess(command, 0, "Passed: 160\n", "")
        with patch.object(tool.subprocess, "run", return_value=failed_count), patch("builtins.print"):
            with self.assertRaisesRegex(ValueError, "expected all 161 extra LLVM lit suite tests"):
                runner.run_lit(command, 161, suite_label="extra LLVM lit suite")

        failed_run = subprocess.CompletedProcess(command, 1, "Passed: 161\nFailed: 1\n", "")
        with patch.object(tool.subprocess, "run", return_value=failed_run), patch("builtins.print"):
            with self.assertRaises(subprocess.CalledProcessError):
                runner.run_lit(command, 161, suite_label="extra LLVM lit suite")

    def test_wrong_pin_fails_before_any_command(self):
        runner = self.planned_runner()
        with patch.object(tool, "PINS", {**self.pins, "revision": "wrong-pin"}):
            with self.assertRaisesRegex(ValueError, "LLVM HEAD"):
                tool.build(self.args(), runner)
        self.assertEqual(runner.call_count, 0)

    def test_modified_tracked_input_fails_without_writing_inputs(self):
        (self.llvm / "source.txt").write_text("changed\n")
        runner = self.planned_runner()
        with self.assertRaisesRegex(ValueError, "modified tracked files"):
            tool.build(self.args(), runner)
        self.assertEqual(runner.call_count, 0)
        self.assertEqual((self.llvm / "source.txt").read_text(), "changed\n")

    def test_initialized_pinned_submodule_is_checked_recursively(self):
        child_source = self.repository("nested-source", "nested")
        subprocess.run(["git", "-C", str(self.llvm), "-c", "protocol.file.allow=always",
                        "submodule", "add", "-q", str(child_source), "nested"], check=True)
        self.git(self.llvm, "add", ".gitmodules", "nested")
        subprocess.run(["git", "-C", str(self.llvm), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "with submodule"],
                       check=True)
        self.pins["revision"] = self.head(self.llvm)
        tool.verify_source(self.llvm, self.pins["revision"], "LLVM")
        export = self.base / "exported-llvm"
        with patch("builtins.print"):
            tool.export_source(self.llvm, export, self.pins["revision"], tool.CommandRunner())
        self.assertEqual((export / "nested/source.txt").read_text(), "nested\n")
        self.assertFalse((export / ".git").exists())
        self.assertFalse((export / "nested/.git").exists())
        self.assertEqual(self.git(self.llvm, "status", "--porcelain"), "")
        (self.llvm / "nested/source.txt").write_text("changed nested\n")
        with self.assertRaisesRegex(ValueError, "modified tracked files"):
            tool.verify_source(self.llvm, self.pins["revision"], "LLVM")

    def test_existing_output_is_never_reused(self):
        self.output.mkdir()
        sentinel = self.output / "keep"
        sentinel.write_text("owned\n")
        with self.assertRaisesRegex(ValueError, "output must be fresh"):
            tool.build(self.args(), self.planned_runner())
        self.assertEqual(sentinel.read_text(), "owned\n")

    def test_std_input_symlink_is_rejected(self):
        link = self.base / "std-link"
        link.symlink_to(self.std, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "independent directory"):
            tool.build(self.args(std_source=link), self.planned_runner())

    def test_std_proposal_validator_accepts_path_and_loaded_ledger(self):
        ledger, ledger_path, _, _ = self.std_provenance_fixture()
        self.assertEqual(tool.validate_std_proposal(self.std, ledger), ledger)
        self.assertEqual(tool.validate_std_proposal(self.std, ledger_path), ledger)

    def test_std_proposal_is_validated_during_explicit_build_preflight(self):
        _, ledger_path, _, _ = self.std_provenance_fixture()
        with patch("builtins.print"):
            tool.build(self.args(std_provenance=ledger_path), self.planned_runner())
        self.assertFalse(self.output.exists())

    def test_std_proposal_validator_rejects_unpinned_metadata_and_digests(self):
        ledger, _, _, _ = self.std_provenance_fixture()
        cases = [
            ("target_os", "linux"),
            ("selection_scope", "default"),
            ("source_revision", "0" * 40),
            ("proposal_manifest_sha256", "0" * 64),
        ]
        for key, value in cases:
            with self.subTest(key=key):
                corrupted = json.loads(json.dumps(ledger))
                corrupted[key] = value
                with self.assertRaises(ValueError):
                    tool.validate_std_proposal(self.std, corrupted)

        corrupted = json.loads(json.dumps(ledger))
        first = next(iter(corrupted["files"].values()))
        first["before_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "before digest"):
            tool.validate_std_proposal(self.std, corrupted)

        corrupted = json.loads(json.dumps(ledger))
        first = next(iter(corrupted["files"].values()))
        first["after_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "after digest"):
            tool.validate_std_proposal(self.std, corrupted)

    def test_std_proposal_validator_rejects_path_ledger_mismatch_and_patch_drift(self):
        ledger, _, proposal_dir, manifest_path = self.std_provenance_fixture()
        corrupted = json.loads(json.dumps(ledger))
        corrupted["files"]["std/src/num/other.rs"] = corrupted["files"].pop(
            "std/src/num/f32.rs")
        with self.assertRaisesRegex(ValueError, "file paths"):
            tool.validate_std_proposal(self.std, corrupted)

        (proposal_dir / "fixture.patch").write_bytes(b"drifted patch\n")
        with self.assertRaisesRegex(ValueError, "patch hash mismatch"):
            tool.validate_std_proposal(self.std, ledger)

        (proposal_dir / "fixture.patch").write_bytes(subprocess.run(
            ["git", "-C", str(self.base / "patch-source"), "diff", "--binary", "HEAD"],
            check=True, capture_output=True).stdout)
        manifest = json.loads(manifest_path.read_text())
        manifest["proposals"]["fixture-rfc"]["before_sha256"][
            "std/src/num/f32.rs"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "proposal_manifest_sha256"):
            tool.validate_std_proposal(self.std, ledger)

    def test_std_proposal_source_after_hashes_must_be_regular_actual_files(self):
        ledger, _, _, _ = self.std_provenance_fixture()
        target = self.std / "std/src/num/f32.rs"
        target.write_bytes(b"not the recorded after bytes\n")
        with self.assertRaisesRegex(ValueError, "after digest"):
            tool.validate_std_proposal(self.std, ledger)
        target.unlink()
        target.symlink_to(self.std / "core/src/lib.rs")
        with self.assertRaisesRegex(ValueError, "after digest"):
            tool.validate_std_proposal(self.std, ledger)

    def test_std_proposal_rejects_source_and_ledger_tampered_together(self):
        ledger, _, _, _ = self.std_provenance_fixture()
        target = self.std / "std/src/num/f32.rs"
        tampered = b"coordinated source and ledger forgery\n"
        target.write_bytes(tampered)
        ledger["files"]["std/src/num/f32.rs"]["after_sha256"] = hashlib.sha256(
            tampered).hexdigest()
        with self.assertRaisesRegex(ValueError, "not the result|reverse"):
            tool.validate_std_proposal(self.std, ledger)

    def test_missing_core_snapshot_fails_during_startup_preflight(self):
        (self.std / "core/src/lib.rs").unlink()
        runner = self.planned_runner()
        with self.assertRaisesRegex(ValueError, "core/src/lib.rs"):
            tool.build(self.args(), runner)
        self.assertEqual(runner.call_count, 0)
        self.assertFalse(self.output.exists())

    def test_packaging_detaches_source_ancestor_symlink_without_following_it(self):
        package = self.base / "package"
        source_tree = self.base / "protected-rust-source"
        protected_library = source_tree / "library/core/src/lib.rs"
        protected_library.parent.mkdir(parents=True)
        protected_library.write_bytes(b"protected upstream core bytes\n")
        (source_tree / "library/marker").write_bytes(b"protected marker\n")
        expected = {p.relative_to(source_tree).as_posix(): tool.sha256(p)
                    for p in (source_tree / "library").rglob("*") if p.is_file()}
        package.mkdir()
        rust_link = package / "lib/rustlib/src/rust"
        rust_link.parent.mkdir(parents=True)
        rust_link.symlink_to(source_tree, target_is_directory=True)
        compiler_source_link = package / "lib/rustlib/rustc-src/rust"
        compiler_source_link.parent.mkdir(parents=True)
        compiler_source_link.symlink_to(source_tree, target_is_directory=True)
        tool.remove_copied_path(package, Path("lib/rustlib/rustc-src/rust"))
        rows = tool.install_std_source(package, self.std)
        library = rust_link / "library"
        self.assertFalse(library.is_symlink())
        self.assertFalse(rust_link.is_symlink())
        self.assertFalse(compiler_source_link.is_symlink())
        self.assertEqual((library / "core/src/lib.rs").read_text(),
                         "// independent core snapshot\n")
        self.assertEqual(len(rows), 1)
        actual = {p.relative_to(source_tree).as_posix(): tool.sha256(p)
                  for p in (source_tree / "library").rglob("*") if p.is_file()}
        self.assertEqual(actual, expected)

    def test_packaged_std_symlink_cannot_escape_snapshot(self):
        outside = self.base / "protected.txt"
        outside.write_text("protected\n")
        (self.std / "escape").symlink_to(outside)
        package = self.base / "package"
        package.mkdir()
        with self.assertRaisesRegex(ValueError, "symlink escapes"):
            tool.install_std_source(package, self.std)
        self.assertEqual(outside.read_text(), "protected\n")


if __name__ == "__main__":
    unittest.main()
