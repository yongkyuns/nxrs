"""Ensure public evidence is reparsed and cannot accept altered private results."""
import hashlib
import json
from pathlib import Path
import statistics
import tempfile
import unittest

import build
import conversion_ranges
import conversion_small_ranges
import generate
import measure
import mul_widths
import report
from test_measure import capture


class ReportTests(unittest.TestCase):
    def test_guarded_casts_compiler_evidence_binds_frozen_parent_and_native_execution(self):
        root = Path(__file__).resolve().parents[2]
        reports = root / 'tests/arithmetic-parity/results'
        evidence = json.loads((reports / 'compiler-guarded-casts-2026-10-06.json').read_text())
        parent = json.loads((reports / 'compiler-signbits-2026-10-06.json').read_text())
        self.assertEqual(evidence['proposal_set'], 'guarded-casts')
        self.assertEqual(evidence['parent_selection'], 'signed-overflow')
        self.assertEqual(evidence['patches'][:20], parent['patches'])
        self.assertEqual(len(evidence['patches']), 22)
        for index, row in enumerate(evidence['patches']):
            folder = 'patches' if index < 6 else 'proposals'
            self.assertEqual(build.digest(root / 'upstream/rust-llvm' / folder / row['name']), row['sha256'])
        self.assertEqual(evidence['llvm_revision'], parent['llvm_revision'])
        self.assertEqual(evidence['rust_revision'], parent['rust_revision'])
        self.assertEqual(evidence['suite_results'], {
            'xtensa': {'PASS': 118}, 'optimizer': {'PASS': 467, 'UNSUPPORTED': 104, 'XFAIL': 3},
            'x86': {'PASS': 7}})
        self.assertEqual(evidence['public_source_verification'], {'files': 65, 'exact_match': True})
        self.assertEqual(evidence['unchanged_std_inventory_sha256'], parent['unchanged_std_inventory_sha256'])
        self.assertEqual(evidence['unchanged_overflow_legalizer_sha256'], parent['changed_legalizer_sha256'])
        native = evidence['host_execution']
        self.assertEqual(native['architecture'], 'x86_64')
        self.assertEqual(native['validator_sha256'], build.digest(root / 'tests/arithmetic-parity/compiler/check_fp_casts.py'))
        for label in ('candidate', 'control'):
            self.assertEqual(sum(native['results'][label]['checks'].values()), 270592)
            self.assertEqual(native['results'][label]['mismatches'], 0)
        self.assertEqual(native['results']['candidate']['llc_sha256'], evidence['llvm_tools_sha256']['llc'])
        self.assertEqual(native['results']['control']['llc_sha256'], parent['llvm_tools_sha256']['llc'])
        self.assertEqual(evidence['old_assembly_check']['expected_failures'], 3)
        self.assertEqual(evidence['full_corpus_machine_verifier']['cases'], 1136)
        self.assertTrue(evidence['full_corpus_machine_verifier']['passed'])
        self.assertNotIn('/Users/', json.dumps(evidence))
        self.assertNotIn('/home/', json.dumps(evidence))

    def test_fpclass_casts_compiler_evidence_binds_guarded_parent(self):
        self.check_cast_compiler_evidence(
            'compiler-fpclass-casts-2026-10-06.json',
            'compiler-guarded-casts-2026-10-06.json',
            'fpclass-casts', 'guarded-casts', 22, 23, 118, 65, 2)

    def test_soft_casts_compiler_evidence_binds_fpclass_parent(self):
        self.check_cast_compiler_evidence(
            'compiler-soft-casts-2026-10-06.json',
            'compiler-fpclass-casts-2026-10-06.json',
            'soft-casts', 'fpclass-casts', 23, 24, 119, 66, 1)

    def test_sign_mask_compiler_evidence_binds_soft_casts_parent(self):
        self.check_cast_compiler_evidence(
            'compiler-sign-mask-2026-10-06.json',
            'compiler-soft-casts-2026-10-06.json',
            'sign-mask', 'soft-casts', 24, 25, 120, 67, 1)

    def test_bit_branch_compiler_evidence_binds_sign_mask_parent_and_both_controls(self):
        evidence = self.check_cast_compiler_evidence(
            'compiler-bit-branch-2026-10-07.json',
            'compiler-sign-mask-2026-10-06.json',
            'bit-branch', 'sign-mask', 25, 27, 122, 72, 1, x86_passes=12)
        self.assertEqual(evidence['selector_without_cleanup_assembly_check']['expected_failures'], 1)
        self.assertRegex(evidence['selector_without_cleanup_assembly_check']['control_llc_sha256'],
                         r'^[0-9a-f]{64}$')
        self.assertRegex(evidence['selector_without_cleanup_assembly_check']['log_sha256'],
                         r'^[0-9a-f]{64}$')
        actual_ir = evidence['rebuilt_driver_full_corpus_machine_verifier']
        self.assertEqual(actual_ir['cases'], 1136)
        self.assertTrue(actual_ir['passed'])
        for key in ('input_ir_sha256', 'object_sha256', 'log_sha256'):
            self.assertRegex(actual_ir[key], r'^[0-9a-f]{64}$')

    def test_zero_select_evidence_binds_driver_and_distinguishes_unchanged_firmware(self):
        evidence = self.check_cast_compiler_evidence(
            'compiler-zero-select-2026-10-07.json',
            'compiler-bit-branch-2026-10-07.json',
            'zero-select', 'bit-branch', 27, 28, 123, 74, 2, x86_passes=18)
        root = Path(__file__).resolve().parents[2]
        parent = json.loads((root / 'tests/arithmetic-parity/results/'
                             'compiler-bit-branch-2026-10-07.json').read_text())
        self.assertNotEqual(evidence['driver_libraries_sha256'], parent['driver_libraries_sha256'])
        native = evidence['zero_select_execution']
        self.assertEqual(native['validator_sha256'],
                         build.digest(root / 'tests/arithmetic-parity/compiler/check_zero_select.py'))
        for label in ('candidate', 'control'):
            self.assertEqual(sum(native['results'][label]['checks'].values()), 4387358)
            self.assertEqual(native['results'][label]['mismatches'], 0)
            self.assertGreater(native['results'][label]['checks']['zero_dynamic_poison'], 0)
            self.assertGreater(native['results'][label]['checks']['zero_dynamic_poison_ne'], 0)
        self.assertEqual(native['results']['candidate']['llc_sha256'],
                         evidence['llvm_tools_sha256']['llc'])
        self.assertEqual(native['results']['control']['llc_sha256'],
                         parent['llvm_tools_sha256']['llc'])
        probe = evidence['rust_value_only_probe']
        self.assertEqual(probe['source_sha256'], build.digest(root / probe['source_path']))
        self.assertEqual(probe['driver_libraries_sha256']['candidate'], evidence['driver_libraries_sha256'])
        self.assertEqual(probe['driver_libraries_sha256']['control'], parent['driver_libraries_sha256'])
        self.assertEqual(probe['functions']['checked_zero'], dict(
            parent_symbol_bytes=103, candidate_symbol_bytes=92,
            parent_instructions=37, candidate_instructions=33))
        for name in ('checked_shared_flag', 'checked_nonzero_default'):
            row = probe['functions'][name]
            self.assertEqual(row['parent_symbol_bytes'], row['candidate_symbol_bytes'])
            self.assertEqual(row['parent_instructions'], row['candidate_instructions'])
        self.assertFalse(probe['device_timing_measured'])
        equivalence = evidence['full_corpus_equivalence']
        for key in ('ir_identical', 'backend_object_identical', 'rust_input_elf_identical'):
            self.assertTrue(equivalence[key])
        self.assertFalse(equivalence['new_final_link'])
        self.assertFalse(equivalence['new_device_measurement'])
        actual_ir = evidence['rebuilt_driver_full_corpus_machine_verifier']
        parent_ir = parent['rebuilt_driver_full_corpus_machine_verifier']
        for key in ('input_ir_sha256', 'object_sha256'):
            self.assertEqual(actual_ir[key], parent_ir[key])
        device = evidence['unchanged_device_report']
        self.assertEqual(device['sha256'], build.digest(root / device['path']))
        self.assertEqual(equivalence['input_elf_sha256'], equivalence['parent_input_elf_sha256'])

    def test_mul_range_evidence_binds_matched_probes_and_unchanged_full_corpus(self):
        evidence = self.check_cast_compiler_evidence(
            'compiler-mul-range-2026-10-07.json',
            'compiler-zero-select-2026-10-07.json',
            'mul-range', 'zero-select', 28, 29, 124, 76, 2, x86_passes=19)
        root = Path(__file__).resolve().parents[2]
        parent = json.loads((root / 'tests/arithmetic-parity/results/'
                             'compiler-zero-select-2026-10-07.json').read_text())
        self.assertNotEqual(evidence['driver_libraries_sha256'], parent['driver_libraries_sha256'])
        native = evidence['mul_range_execution']
        self.assertEqual(native['validator_sha256'],
                         build.digest(root / 'tests/arithmetic-parity/compiler/check_mul_ranges.py'))
        for label in ('candidate', 'control'):
            row = native['results'][label]
            self.assertEqual(sum(row['checks'].values()), 172522)
            self.assertEqual(row['mismatches'], 0)
            self.assertTrue(all(row['overflow_cases'].values()))
        self.assertEqual(native['results']['candidate']['llc_sha256'],
                         evidence['llvm_tools_sha256']['llc'])
        self.assertEqual(native['results']['control']['llc_sha256'],
                         parent['llvm_tools_sha256']['llc'])
        probe = evidence['rust_range_probe']
        self.assertEqual(probe['source_sha256'], build.digest(root / probe['source_path']))
        c = probe['c_control']
        self.assertEqual(c['source_sha256'], build.digest(root / c['source_path']))
        self.assertEqual(probe['driver_libraries_sha256']['candidate'], evidence['driver_libraries_sha256'])
        self.assertEqual(probe['driver_libraries_sha256']['control'], parent['driver_libraries_sha256'])
        for name, sizes, instructions in (
                ('checked_unsigned_halves', (59, 27, 75), (22, 10, 27)),
                ('checked_masked_halves', (57, 25, 63), (21, 9, 24)),
                ('checked_full_width', (106, 106, 179), (38, 38, 69))):
            row = probe['functions'][name]
            self.assertEqual(tuple(row[key + '_symbol_bytes'] for key in
                                   ('parent', 'candidate', 'c')), sizes)
            self.assertEqual(tuple(row[key + '_instructions'] for key in
                                   ('parent', 'candidate', 'c')), instructions)
        self.assertFalse(probe['device_timing_measured'])
        equivalence = evidence['full_corpus_equivalence']
        for key in ('ir_identical', 'backend_object_identical', 'rust_input_elf_identical'):
            self.assertTrue(equivalence[key])
        for key in ('new_final_link', 'new_device_measurement'):
            self.assertFalse(equivalence[key])
        self.assertEqual(equivalence['input_elf_sha256'], equivalence['parent_input_elf_sha256'])
        for key in ('input_ir_sha256', 'object_sha256'):
            self.assertEqual(evidence['rebuilt_driver_full_corpus_machine_verifier'][key],
                             parent['rebuilt_driver_full_corpus_machine_verifier'][key])
        device = evidence['unchanged_device_report']
        self.assertEqual(device['sha256'], build.digest(root / device['path']))

    def test_guarded_casts_device_cohorts_keep_kernel_std_and_c_controls_fixed(self):
        root = Path(__file__).resolve().parents[2]
        reports = root / 'tests/arithmetic-parity/results'
        compiler = json.loads((reports / 'compiler-guarded-casts-2026-10-06.json').read_text())
        cohorts = (
            ('esp32s3-signbits-math-2026-10-06.json',
             'esp32s3-guarded-casts-math-2026-10-06.json', 1136, 942),
            ('esp32s3-cast-ranges-control-2026-10-06.json',
             'esp32s3-cast-ranges-candidate-2026-10-06.json', 16, 16),
        )
        self.check_device_compiler_cohorts(root, reports, compiler, cohorts,
                                          'guarded-casts', 20, 22, 118, 474)
        for label in ('control', 'candidate'):
            evidence = json.loads((reports / f'esp32s3-cast-ranges-{label}-2026-10-06.json').read_text())
            self.assertEqual([row['name'] for row in evidence['cases']],
                             [row['name'] for row in conversion_ranges.catalog()])
            self.assertEqual(evidence['totals']['raw_pair_differences'], 0)
            self.assertEqual(evidence['source_sha256']['conversion_ranges.py'],
                             build.digest(root / 'tests/arithmetic-parity/conversion_ranges.py'))

    def test_fpclass_casts_device_cohorts_bind_compiler_and_fixed_inputs(self):
        root = Path(__file__).resolve().parents[2]
        reports = root / 'tests/arithmetic-parity/results'
        compiler = json.loads((reports / 'compiler-fpclass-casts-2026-10-06.json').read_text())
        cohorts = (
            ('esp32s3-guarded-casts-math-2026-10-06.json',
             'esp32s3-fpclass-casts-math-2026-10-06.json', 1136, 942),
            ('esp32s3-cast-ranges-candidate-2026-10-06.json',
             'esp32s3-fpclass-cast-ranges-2026-10-06.json', 16, 16),
        )
        self.check_device_compiler_cohorts(
            root, reports, compiler, cohorts, 'fpclass-casts', 22, 23, 118, 474)
        self.assert_range_device_catalogs(root, reports,
                                          'esp32s3-fpclass-cast-ranges-2026-10-06.json',
                                          conversion_ranges.catalog(),
                                          ('conversion_ranges.py',))

    def test_soft_casts_device_cohorts_bind_compiler_and_fixed_inputs(self):
        root = Path(__file__).resolve().parents[2]
        reports = root / 'tests/arithmetic-parity/results'
        compiler = json.loads((reports / 'compiler-soft-casts-2026-10-06.json').read_text())
        cohorts = (
            ('esp32s3-fpclass-casts-math-2026-10-06.json',
             'esp32s3-soft-casts-math-2026-10-06.json', 1136, 942),
            ('esp32s3-small-cast-ranges-control-2026-10-06.json',
             'esp32s3-small-cast-ranges-candidate-2026-10-06.json', 48, 48),
        )
        self.check_device_compiler_cohorts(
            root, reports, compiler, cohorts, 'soft-casts', 23, 24, 119, 474)
        for label in ('control', 'candidate'):
            self.assert_range_device_catalogs(
                root, reports, f'esp32s3-small-cast-ranges-{label}-2026-10-06.json',
                conversion_small_ranges.catalog(),
                ('conversion_small_ranges.py', 'conversion_ranges.py'))

    def test_sign_mask_device_cohort_binds_compiler_and_fixed_inputs(self):
        root = Path(__file__).resolve().parents[2]
        reports = root / 'tests/arithmetic-parity/results'
        compiler = json.loads((reports / 'compiler-sign-mask-2026-10-06.json').read_text())
        cohorts = (
            ('esp32s3-soft-casts-math-2026-10-06.json',
             'esp32s3-sign-mask-math-2026-10-06.json', 1136, 942),
        )
        self.check_device_compiler_cohorts(
            root, reports, compiler, cohorts, 'sign-mask', 24, 25, 120, 474)

    def test_bit_branch_device_cohort_binds_compiler_and_fixed_inputs(self):
        root = Path(__file__).resolve().parents[2]
        reports = root / 'tests/arithmetic-parity/results'
        compiler = json.loads((reports / 'compiler-bit-branch-2026-10-07.json').read_text())
        cohorts = (
            ('esp32s3-sign-mask-math-2026-10-06.json',
             'esp32s3-bit-branch-math-2026-10-07.json', 1136, 942),
        )
        self.check_device_compiler_cohorts(
            root, reports, compiler, cohorts, 'bit-branch', 25, 27, 122, 479)

    def test_signbits_compiler_evidence_binds_generic_model_and_parent(self):
        root = Path(__file__).resolve().parents[2]
        reports = root / "tests/arithmetic-parity/results"
        evidence = json.loads((reports / "compiler-signbits-2026-10-06.json").read_text())
        parent = json.loads((reports / "compiler-zero-compare-2026-10-06.json").read_text())
        device = json.loads((reports / "esp32s3-zero-compare-math-2026-10-06.json").read_text())
        pins = json.loads((root / "upstream/rust-llvm/upstream.json").read_text())
        self.assertEqual(evidence["proposal_set"], "signed-overflow")
        self.assertEqual(evidence["parent_selection"], "zero-compare")
        self.assertEqual(evidence["llvm_revision"], pins["revision"])
        self.assertEqual(evidence["rust_revision"], pins["rust_revision"])
        self.assertEqual(evidence["patches"][:-1], parent["patches"])
        self.assertEqual(len(evidence["patches"]), 20)
        self.assertNotIn(evidence["excluded_proposal"],
                         [row["name"] for row in evidence["patches"]])
        for index, row in enumerate(evidence["patches"]):
            folder = "patches" if index < 6 else "proposals"
            self.assertEqual(build.digest(root / "upstream/rust-llvm" / folder / row["name"]),
                             row["sha256"])
        self.assertEqual(evidence["suite_results"], {
            "xtensa": {"PASS": 116}, "optimizer": {"PASS": 409, "UNSUPPORTED": 50, "XFAIL": 3},
            "x86": {"PASS": 6}})
        self.assertEqual(evidence["public_source_verification"], {"files": 60, "exact_match": True})
        self.assertEqual(evidence["unchanged_std_inventory_sha256"], device["build"]["std_inventory_sha256"])
        model = evidence["arithmetic_model"]
        self.assertEqual(model["sha256"], build.digest(root / model["path"]))
        self.assertEqual(sum(model["checks"].values()), 1242336)
        self.assertEqual(model["mismatches"], 0)
        self.assertIn("not compiler or device execution", model["limitation"])
        native = evidence["host_execution"]
        self.assertEqual(native["architecture"], "x86_64")
        for label in ("candidate", "control"):
            row = native["results"][label]
            self.assertEqual(row["boundary"] + row["full_width_random"] + row["random_width_pairs"], 60841)
            self.assertEqual(row["mismatches"], 0)
        self.assertEqual(native["results"]["candidate"]["llc_sha256"], evidence["llvm_tools_sha256"]["llc"])
        self.assertEqual(native["results"]["control"]["llc_sha256"], parent["llvm_tools_sha256"]["llc"])
        self.assertEqual(evidence["old_assembly_check"]["expected_failures"], 2)
        self.assertEqual(evidence["full_corpus_machine_verifier"]["cases"], 1136)
        self.assertTrue(evidence["full_corpus_machine_verifier"]["passed"])
        self.assertNotIn("/Users/", json.dumps(evidence))
        self.assertNotIn("/home/", json.dumps(evidence))

    def test_zero_compare_compiler_evidence_excludes_rejected_width_pass(self):
        root = Path(__file__).resolve().parents[2]
        reports = root / "tests/arithmetic-parity/results"
        evidence = json.loads((reports / "compiler-zero-compare-2026-10-06.json").read_text())
        parent = json.loads((reports / "compiler-mixed-mul-2026-10-06.json").read_text())
        pins = json.loads((root / "upstream/rust-llvm/upstream.json").read_text())
        self.assertEqual(evidence["proposal_set"], "zero-compare")
        self.assertEqual(evidence["parent_selection"], "mixed-mul")
        self.assertEqual(evidence["llvm_revision"], pins["revision"])
        self.assertEqual(evidence["rust_revision"], pins["rust_revision"])
        self.assertEqual(evidence["patches"][:-1], parent["patches"])
        self.assertEqual(len(evidence["patches"]), 19)
        self.assertNotIn(evidence["excluded_proposal"],
                         [row["name"] for row in evidence["patches"]])
        self.assertEqual(evidence["patches"][-1]["name"],
                         "0020-Xtensa-select-zero-comparisons-directly.patch")
        for index, row in enumerate(evidence["patches"]):
            folder = "patches" if index < 6 else "proposals"
            self.assertEqual(build.digest(root / "upstream/rust-llvm" / folder / row["name"]),
                             row["sha256"])
        self.assertEqual(evidence["suite_results"], {
            "xtensa": {"PASS": 115},
            "optimizer": {"PASS": 409, "UNSUPPORTED": 50, "XFAIL": 3},
            "x86": {"PASS": 5}})
        self.assertEqual(evidence["public_source_verification"], {"files": 57, "exact_match": True})
        self.assertEqual(evidence["unchanged_wide_overflow_lowering_sha256"],
                         "80d8e74160c4cea06edfce5736c42129f1f017b62f903c79138330f864a4ff2c")
        self.assertEqual(evidence["old_assembly_check"]["expected_failures"], 1)
        self.assertEqual(evidence["full_corpus_machine_verifier"]["cases"], 1136)
        self.assertTrue(evidence["full_corpus_machine_verifier"]["passed"])
        self.assertIn("Backend object verification is not device execution",
                      evidence["full_corpus_machine_verifier"]["limitation"])
        self.assertNotIn("/Users/", json.dumps(evidence))
        self.assertNotIn("/home/", json.dumps(evidence))

    def test_mixed_mul_compiler_evidence_binds_patch_chain_and_frozen_std(self):
        root = Path(__file__).resolve().parents[2]
        reports = root / "tests/arithmetic-parity/results"
        path = reports / "compiler-mixed-mul-2026-10-06.json"
        evidence = json.loads(path.read_text())
        parent = json.loads((reports / "compiler-hardening-2026-10-06.json").read_text())
        device = json.loads((reports / "esp32s3-constant-hwloop-math-2026-10-06.json").read_text())
        pins = json.loads((root / "upstream/rust-llvm/upstream.json").read_text())
        self.assertEqual(evidence["proposal_set"], "mixed-mul")
        self.assertEqual(evidence["llvm_revision"], pins["revision"])
        self.assertEqual(evidence["rust_revision"], pins["rust_revision"])
        self.assertEqual(len(evidence["patches"]), 18)
        self.assertEqual(evidence["patches"][:-1], parent["patches"])
        for index, row in enumerate(evidence["patches"]):
            folder = "patches" if index < 6 else "proposals"
            self.assertEqual(build.digest(root / "upstream/rust-llvm" / folder / row["name"]),
                             row["sha256"])
        self.assertEqual(evidence["suite_results"], {
            "xtensa": {"PASS": 114},
            "optimizer": {"PASS": 409, "UNSUPPORTED": 50, "XFAIL": 3},
            "x86": {"PASS": 5},
        })
        self.assertEqual(evidence["public_source_verification"], {"files": 56, "exact_match": True})
        self.assertEqual(evidence["unchanged_std_inventory_sha256"],
                         device["build"]["std_inventory_sha256"])
        model = evidence["arithmetic_model"]
        self.assertEqual(model["sha256"], build.digest(root / model["path"]))
        self.assertEqual(model["mismatches"], 0)
        native = evidence["host_execution"]
        self.assertEqual(native["architecture"], "x86_64")
        self.assertEqual(native["mixed_calls_per_compiler"], 20616)
        self.assertEqual(native["signed128_overflow_pairs_per_compiler"], 10841)
        self.assertEqual(native["mismatches"], 0)
        self.assertEqual(evidence["old_compiler_regression"]["expected_failed_tests"], 1)
        self.assertNotIn("/Users/", path.read_text())
        self.assertNotIn("/home/", path.read_text())

    def test_tests_only_hardening_evidence_keeps_device_report_and_driver_frozen(self):
        root = Path(__file__).resolve().parents[2]
        reports = root / "tests/arithmetic-parity/results"
        path = reports / "compiler-hardening-2026-10-06.json"
        evidence = json.loads(path.read_text())
        device_path = root / evidence["unchanged_device_report"]["path"]
        device = json.loads(device_path.read_text())
        pins = json.loads((root / "upstream/rust-llvm/upstream.json").read_text())
        self.assertEqual(evidence["proposal_set"], "hardening")
        self.assertEqual(evidence["parent_selection"], "constant-hwloop")
        self.assertEqual(evidence["llvm_revision"], pins["revision"])
        self.assertEqual(evidence["rust_revision"], pins["rust_revision"])
        self.assertEqual(build.digest(device_path),
                         evidence["unchanged_device_report"]["sha256"])
        self.assertEqual({str(Path("lib") / name): sha for name, sha in
                          evidence["unchanged_driver_sha256"].items()},
                         device["build"]["rust_driver_libraries_sha256"])
        self.assertEqual(evidence["patches"][:-1],
                         [{key: row[key] for key in ("name", "sha256")}
                          for row in device["build"]["patch_ledger"]["patches"]])
        self.assertEqual(len(evidence["patches"]), 17)
        for index, row in enumerate(evidence["patches"]):
            folder = "patches" if index < 6 else "proposals"
            self.assertEqual(build.digest(root / "upstream/rust-llvm" / folder / row["name"]),
                             row["sha256"])
        manifest_path = root / "upstream/rust-llvm/proposals/series.json"
        selection = json.loads(manifest_path.read_text())["sets"]["hardening"]
        self.assertEqual(selection["qualification_tests"], 113)
        self.assertEqual(selection["extra_test_suites"][0]["tests"], 409)
        self.assertEqual(evidence["suite_results"],
                         {"xtensa": {"PASS": 113},
                          "optimizer": {"PASS": 409, "UNSUPPORTED": 50, "XFAIL": 3}})
        self.assertEqual(evidence["public_source_verification"]["files"], 52)
        self.assertTrue(evidence["public_source_verification"]["exact_match"])
        self.assertEqual(len(evidence["tests_only_new_files"]), 4)
        for name, hashes in evidence["tests_only_new_files"].items():
            self.assertTrue(name.startswith("llvm/test/"))
            self.assertIsNone(hashes["before"])
            self.assertRegex(hashes["after"], r"^[0-9a-f]{64}$")
        wrong = evidence["deliberate_wrong_output_checks"]
        self.assertEqual(wrong["accepted"], 0)
        self.assertEqual(wrong["rejected"], len(wrong["cases"]))
        self.assertEqual(len(set(wrong["cases"])), 27)
        execution = evidence["host_liveout_execution"]
        self.assertEqual(execution["architecture"], "x86_64")
        self.assertEqual(execution["cases_by_ir_stage"],
                         {"original": 168, "indvars": 168, "lsr": 168})
        self.assertEqual(execution["mismatches"], 0)
        self.assertNotIn("/Users/", path.read_text())
        self.assertNotIn("/home/", path.read_text())

    def test_published_device_evidence_is_complete_and_matches_frozen_sources(self):
        for name in ("esp32s3-2026-10-06.json","esp32s3-setlt-2026-10-06.json",
                     "esp32s3-wide-iv-2026-10-06.json","esp32s3-wide-exit-2026-10-06.json",
                     "esp32s3-conditional-move-2026-10-06.json",
                     "esp32s3-std-math-2026-10-06.json",
                     "esp32s3-paired-branch-2026-10-06.json",
                     "esp32s3-paired-branch-math-2026-10-06.json",
                     "esp32s3-constant-hwloop-math-2026-10-06.json",
                     "esp32s3-mixed-mul-math-2026-10-06.json"):
            with self.subTest(report=name):
                self.check_published_evidence(Path(__file__).resolve().parent/"results"/name)

    def check_published_evidence(self, path, case_count=1136, c_case_count=942):
        evidence=json.loads(path.read_text())
        self.assertTrue(evidence["passed"])
        self.assertTrue(evidence["restoration_verified"])
        self.assertEqual(len(evidence["cases"]),case_count)
        self.assertEqual(sum(row["c_available"] for row in evidence["cases"]),c_case_count)
        self.assertEqual(evidence["runs"],3)
        self.assertEqual([row["id"] for row in evidence["cases"]],list(range(case_count)))
        for name,sha in evidence["source_sha256"].items():
            self.assertEqual(build.digest(path.parents[1]/name),sha)
        totals={"c_errors":0,"rust_errors":0,"raw_pair_differences":0}
        for row in evidence["cases"]:
            self.assertEqual(len(row["correctness"]),3)
            for result in row["correctness"]:
                totals["c_errors"]+=result["c_errors"]
                totals["rust_errors"]+=result["rust_errors"]
                totals["raw_pair_differences"]+=result["pair_diff"]
            for mode in ("masked","normal"):
                for backend in ("c","rust"):
                    runs=row["cycles"][mode][backend]
                    self.assertEqual(len(runs),3)
                    self.assertTrue(all(len(run)==5 for run in runs))
                    if backend=="c" and not row["c_available"]:
                        self.assertTrue(all(value==0 for run in runs for value in run))
                    else:
                        self.assertTrue(all(value>0 for run in runs for value in run))
        self.check_masked_timing_metrics(evidence)
        self.assertEqual(totals,evidence["totals"])
        self.assertEqual(totals["c_errors"],0)
        self.assertEqual(totals["rust_errors"],0)
        self.assertNotIn("/Users/",path.read_text())
        self.assertNotIn("/home/",path.read_text())

    def check_masked_timing_metrics(self,evidence):
        """Recompute exported medians and ratios from every recorded masked repeat."""
        for row in evidence["cases"]:
            with self.subTest(case=row["id"]):
                self.check_masked_case_metrics(row)

    def check_masked_case_metrics(self,row):
        c=statistics.median(value for run in row["cycles"]["masked"]["c"]
                            for value in run)
        rust=statistics.median(value for run in row["cycles"]["masked"]["rust"]
                               for value in run)
        self.assertEqual(row["masked_median_cycles"]["rust"],rust)
        if row["c_available"]:
            self.assertEqual(row["masked_median_cycles"]["c"],c)
            self.assertAlmostEqual(row["rust_c_ratio"],rust/c,places=12)
        else:
            self.assertIsNone(row["masked_median_cycles"]["c"])
            self.assertIsNone(row["rust_c_ratio"])

    def test_masked_timing_assertion_rejects_tampered_derived_values(self):
        path=Path(__file__).resolve().parent/"results/esp32s3-mul-width-candidate-2026-10-06.json"
        evidence=json.loads(path.read_text())
        self.check_masked_timing_metrics(evidence)
        tampered=json.loads(json.dumps(evidence))
        tampered["cases"][0]["masked_median_cycles"]["rust"]+=1
        with self.assertRaises(AssertionError):
            self.check_masked_case_metrics(tampered["cases"][0])

        no_c=json.loads(json.dumps(evidence))
        no_c["cases"][0]["c_available"]=False
        no_c["cases"][0]["masked_median_cycles"]["c"]=None
        no_c["cases"][0]["rust_c_ratio"]=None
        self.check_masked_case_metrics(no_c["cases"][0])
        no_c["cases"][0]["rust_c_ratio"]=1.0
        with self.assertRaises(AssertionError):
            self.check_masked_case_metrics(no_c["cases"][0])

    def test_setlt_evidence_binds_current_patches_and_preserves_control_inputs(self):
        root=Path(__file__).resolve().parents[2]
        reports=root/"tests/arithmetic-parity/results"
        baseline=json.loads((reports/"esp32s3-2026-10-06.json").read_text())
        candidate=json.loads((reports/"esp32s3-setlt-2026-10-06.json").read_text())
        self.check_control_inputs(baseline,candidate)
        before,after=baseline["build"],candidate["build"]
        ledger=after["patch_ledger"]
        self.assertEqual(ledger["proposal_set"],"setlt")
        self.assertEqual(ledger["qualification_tests"],104)
        self.assertEqual(len(ledger["patches"]),8)
        self.assertEqual(ledger["patches"][:-1],before["patch_ledger"]["patches"])
        self.check_patch_chain(root,ledger)

    def test_wide_iv_evidence_binds_full_public_chain_and_extra_qualification(self):
        self.check_optimizer_followup("wide-iv","esp32s3-setlt-2026-10-06.json",9)

    def test_wide_exit_evidence_binds_full_public_chain_and_extra_qualification(self):
        self.check_optimizer_followup("wide-exit","esp32s3-wide-iv-2026-10-06.json",10)

    def test_conditional_move_evidence_binds_wide_exit_controls_and_full_13patch_chain(self):
        root=Path(__file__).resolve().parents[2]
        reports=root/"tests/arithmetic-parity/results"
        parent=json.loads((reports/"esp32s3-wide-exit-2026-10-06.json").read_text())
        candidate=json.loads((reports/"esp32s3-conditional-move-2026-10-06.json").read_text())
        self.check_control_inputs(parent,candidate)
        parent_ledger=parent["build"]["patch_ledger"]
        ledger=candidate["build"]["patch_ledger"]
        self.assertEqual(ledger["patches"][:len(parent_ledger["patches"])],parent_ledger["patches"])
        self.check_llvm_selection(root,ledger,"conditional-move",108,399)

    def test_paired_branch_evidence_binds_conditional_move_and_full_14patch_chain(self):
        root=Path(__file__).resolve().parents[2]
        reports=root/"tests/arithmetic-parity/results"
        parent=json.loads((reports/"esp32s3-conditional-move-2026-10-06.json").read_text())
        candidate=json.loads((reports/"esp32s3-paired-branch-2026-10-06.json").read_text())
        self.check_compiler_followup(root,parent,candidate,"paired-branch",
                                     parent_patch_count=13,patch_count=14,
                                     qualification_tests=109,optimizer_passes=399)

    def test_std_math_evidence_keeps_compiler_and_llvm_fixed_and_binds_std_proposal(self):
        root=Path(__file__).resolve().parents[2]
        reports=root/"tests/arithmetic-parity/results"
        baseline=json.loads((reports/"esp32s3-conditional-move-2026-10-06.json").read_text())
        candidate=json.loads((reports/"esp32s3-std-math-2026-10-06.json").read_text())
        self.check_std_math_followup(root,baseline,candidate,"conditional-move",108,399)

    def test_paired_branch_math_evidence_keeps_compiler_fixed_and_reuses_std_math_snapshot(self):
        root=Path(__file__).resolve().parents[2]
        reports=root/"tests/arithmetic-parity/results"
        baseline=json.loads((reports/"esp32s3-paired-branch-2026-10-06.json").read_text())
        candidate=json.loads((reports/"esp32s3-paired-branch-math-2026-10-06.json").read_text())
        std_math=json.loads((reports/"esp32s3-std-math-2026-10-06.json").read_text())
        self.check_std_math_followup(root,baseline,candidate,"paired-branch",109,399)
        self.assertEqual(len(candidate["build"]["patch_ledger"]["patches"]),14)
        for key in ("std_inventory_sha256","std_proposal_ledger"):
            self.assertEqual(candidate["build"][key],std_math["build"][key],key)

    def test_constant_hwloop_math_evidence_binds_paired_branch_and_full_16patch_chain(self):
        root=Path(__file__).resolve().parents[2]
        reports=root/"tests/arithmetic-parity/results"
        baseline=json.loads((reports/"esp32s3-paired-branch-math-2026-10-06.json").read_text())
        candidate=json.loads((reports/"esp32s3-constant-hwloop-math-2026-10-06.json").read_text())
        self.check_compiler_followup(root,baseline,candidate,"constant-hwloop",
                                     parent_patch_count=14,patch_count=16,
                                     qualification_tests=111,optimizer_passes=407)
        self.assertEqual(candidate["build"]["std_proposal_ledger"],
                         baseline["build"]["std_proposal_ledger"])
        self.check_std_proposal_ledger(root,candidate["build"]["std_proposal_ledger"])
        ledger=candidate["build"]["patch_ledger"]
        self.assertEqual([row["name"] for row in ledger["patches"][14:]],[
            "0015-Xtensa-use-Boolean-moves-for-FP-integer-selects.patch",
            "0016-HardwareLoops-admit-fitting-wide-constant-counts.patch"])
        # The public ledger exports only the pass expectation, not the discovery
        # counts of 50 Unsupported and 3 XFAIL results.
        self.assertEqual(ledger["extra_qualification"]["test_suites"],[dict(
            paths=["llvm/test/Analysis/IVUsers","llvm/test/Transforms/LoopStrengthReduce",
                   "llvm/test/Transforms/IndVarSimplify","llvm/test/Transforms/HardwareLoops"],
            expected_passes=407,count_scope="llvm-lit Passed count")])

    def check_std_math_followup(self,root,baseline,candidate,selection_name,
                                qualification_tests,optimizer_passes):
        self.check_comparison_controls(baseline,candidate,include_kernel_inventory=True)
        before,after=baseline["build"],candidate["build"]
        self.assertEqual(after["rust_compiler_sha256"],before["rust_compiler_sha256"])
        self.assertEqual(after["rust_driver_libraries_sha256"],before["rust_driver_libraries_sha256"])
        self.assertEqual(after["patch_ledger"],before["patch_ledger"])
        self.check_llvm_selection(root,after["patch_ledger"],selection_name,
                                  qualification_tests,optimizer_passes)
        self.assertNotEqual(after["std_inventory_sha256"],before["std_inventory_sha256"])
        self.check_std_proposal_ledger(root,after["std_proposal_ledger"])

    def check_compiler_followup(self,root,baseline,candidate,selection_name,
                                parent_patch_count,patch_count,qualification_tests,optimizer_passes):
        self.check_comparison_controls(baseline,candidate,include_kernel_inventory=True,
                                       include_std_inventory=True)
        before,after=baseline["build"],candidate["build"]
        self.assertNotEqual(after["rust_driver_libraries_sha256"],
                            before["rust_driver_libraries_sha256"],"Rust driver must change")
        parent_ledger,ledger=before["patch_ledger"],after["patch_ledger"]
        self.assertEqual(len(parent_ledger["patches"]),parent_patch_count)
        self.assertEqual(len(ledger["patches"]),patch_count)
        self.assertEqual(ledger["patches"][:parent_patch_count],parent_ledger["patches"])
        self.check_llvm_selection(root,ledger,selection_name,qualification_tests,optimizer_passes)

    def check_cast_compiler_evidence(self, evidence_name, parent_name, selection,
                                     parent_selection, parent_patch_count, patch_count,
                                     qualification_tests, source_files,
                                     expected_old_failures, x86_passes=7):
        root = Path(__file__).resolve().parents[2]
        reports = root / 'tests/arithmetic-parity/results'
        evidence = json.loads((reports / evidence_name).read_text())
        parent = json.loads((reports / parent_name).read_text())
        pins = json.loads((root / 'upstream/rust-llvm/upstream.json').read_text())

        self.assertEqual(evidence['proposal_set'], selection)
        self.assertEqual(evidence['parent_selection'], parent_selection)
        self.assertEqual(evidence['llvm_revision'], pins['revision'])
        self.assertEqual(evidence['rust_revision'], pins['rust_revision'])
        self.assertEqual(evidence['patches'][:parent_patch_count], parent['patches'])
        self.assertEqual(len(parent['patches']), parent_patch_count)
        self.assertEqual(len(evidence['patches']), patch_count)
        for index, row in enumerate(evidence['patches']):
            folder = 'patches' if index < 6 else 'proposals'
            self.assertEqual(build.digest(root / 'upstream/rust-llvm' / folder / row['name']),
                             row['sha256'])

        self.assertEqual(evidence['suite_results'], {
            'xtensa': {'PASS': qualification_tests},
            'optimizer': {'PASS': 467, 'UNSUPPORTED': 104, 'XFAIL': 3},
            'x86': {'PASS': x86_passes},
        })
        self.assertEqual(evidence['public_source_verification'],
                         {'files': source_files, 'exact_match': True})
        self.assertEqual(evidence['unchanged_std_inventory_sha256'],
                         parent['unchanged_std_inventory_sha256'])
        self.assertEqual(evidence['unchanged_overflow_legalizer_sha256'],
                         parent['unchanged_overflow_legalizer_sha256'])
        self.assertRegex(evidence['guarded_conversion_source_sha256'], r'^[0-9a-f]{64}$')
        self.assertEqual(evidence['host_execution']['source_ir_sha256'],
                         parent['host_execution']['source_ir_sha256'])

        native = evidence['host_execution']
        self.assertEqual(native['architecture'], 'x86_64')
        self.assertEqual(native['validator_sha256'],
                         build.digest(root / 'tests/arithmetic-parity/compiler/check_fp_casts.py'))
        for label in ('candidate', 'control'):
            result = native['results'][label]
            self.assertEqual(sum(result['checks'].values()), 270592)
            self.assertEqual(result['mismatches'], 0)
        self.assertEqual(native['results']['candidate']['llc_sha256'],
                         evidence['llvm_tools_sha256']['llc'])
        self.assertEqual(native['results']['control']['llc_sha256'],
                         parent['llvm_tools_sha256']['llc'])
        self.assertEqual(evidence['old_assembly_check']['expected_failures'],
                         expected_old_failures)
        verifier = evidence['full_corpus_machine_verifier']
        self.assertEqual(verifier['cases'], 1136)
        self.assertTrue(verifier['passed'])
        self.assertEqual(verifier['input_ir_sha256'],
                         parent['full_corpus_machine_verifier']['input_ir_sha256'])

        manifest = json.loads((root / 'upstream/rust-llvm/proposals/series.json').read_text())
        proposal = manifest['sets'][selection]
        self.assertEqual(proposal['qualification_tests'], qualification_tests)
        self.assertEqual([row['name'] for row in evidence['patches'][6:]],
                         proposal['patches'])
        self.assertEqual(sum(suite['tests'] for suite in proposal['extra_test_suites']),
                         467 + x86_passes)
        serialized = json.dumps(evidence)
        self.assertNotIn('/Users/', serialized)
        self.assertNotIn('/home/', serialized)
        return evidence

    def assert_range_device_catalogs(self, root, reports, report_name, catalog, source_names):
        evidence = json.loads((reports / report_name).read_text())
        self.assertEqual([row['name'] for row in evidence['cases']],
                         [row['name'] for row in catalog])
        self.assertEqual(evidence['totals']['raw_pair_differences'], 0)
        for name in source_names:
            self.assertEqual(evidence['source_sha256'][name],
                             build.digest(root / 'tests/arithmetic-parity' / name))

    def check_optimizer_followup(self, selection_name, baseline_name, patch_count):
        root=Path(__file__).resolve().parents[2]
        reports=root/"tests/arithmetic-parity/results"
        baseline=json.loads((reports/baseline_name).read_text())
        candidate=json.loads((reports/f"esp32s3-{selection_name}-2026-10-06.json").read_text())
        self.check_control_inputs(baseline,candidate)
        ledger=candidate["build"]["patch_ledger"]
        self.assertEqual(len(ledger["patches"]),patch_count)
        self.assertEqual(ledger["patches"][:-1],baseline["build"]["patch_ledger"]["patches"])
        self.check_llvm_selection(root,ledger,selection_name,104,
                                  161 if selection_name=="wide-iv" else 399)

    def check_llvm_selection(self,root,ledger,selection_name,qualification_tests,optimizer_passes):
        selection=json.loads((root/"upstream/rust-llvm/proposals/series.json").read_text())["sets"][selection_name]
        self.assertEqual(ledger["proposal_set"],selection_name)
        self.assertEqual(ledger["qualification_tests"],qualification_tests)
        self.assertEqual(selection["qualification_tests"],qualification_tests)
        proposal_patches=selection["patches"]
        self.assertEqual([row["name"] for row in ledger["patches"][-len(proposal_patches):]],proposal_patches)
        self.assertEqual(ledger["extra_qualification"]["before_sha256"],selection["before_sha256"])
        self.assertEqual(ledger["extra_qualification"]["test_suites"],[dict(
            paths=suite["paths"],expected_passes=suite["tests"],count_scope="llvm-lit Passed count")
            for suite in selection["extra_test_suites"]])
        self.assertEqual(sum(suite["tests"] for suite in selection["extra_test_suites"]),optimizer_passes)
        self.check_patch_chain(root,ledger)

    def check_comparison_controls(self,baseline,candidate,include_kernel_inventory,
                                  include_std_inventory=False):
        self.assertEqual(candidate["coverage_sha256"],baseline["coverage_sha256"])
        self.assertEqual(candidate["source_sha256"],baseline["source_sha256"])
        keys=("kernel_config_sha256","c_compiler_sha256","c_flags","target_sha256",
              "std_features","application_opt_level")
        if include_kernel_inventory:
            keys+=("kernel_libraries_inventory_sha256",)
        if include_std_inventory:
            keys+=("std_inventory_sha256",)
        for key in keys:
            self.assertEqual(candidate["build"][key],baseline["build"][key],key)

    def check_std_proposal_ledger(self,root,ledger):
        manifest_path=root/"upstream/rust-std/proposals/series.json"
        manifest=json.loads(manifest_path.read_text())
        proposal=manifest["proposals"][ledger["proposal"]]
        self.assertEqual(ledger["proposal_manifest_sha256"],build.digest(manifest_path))
        for key in ("source_revision","target_os","qualification","selection_scope"):
            self.assertEqual(ledger[key],proposal[key],key)
        self.assertEqual(ledger["proposal_state"],proposal["state"])
        self.assertEqual(ledger["patch"],proposal["patch"])
        self.assertEqual(build.digest(root/"upstream/rust-std/proposals"/proposal["patch"]["file"]),
                         proposal["patch"]["sha256"])
        self.assertEqual(set(ledger["files"]),set(proposal["before_sha256"]))
        self.assertEqual({path:row["before_sha256"] for path,row in ledger["files"].items()},
                         proposal["before_sha256"])
        self.assertEqual(len(ledger["files"]),3)
        for row in ledger["files"].values():
            self.assertRegex(row["after_sha256"],r"^[0-9a-f]{64}$")

    def check_control_inputs(self, baseline, candidate):
        for key in ("coverage_sha256","source_sha256"):
            self.assertEqual(candidate[key],baseline[key])
        for key in ("kernel_config_sha256","c_compiler_sha256","c_flags",
                    "target_sha256","std_inventory_sha256","std_features",
                    "application_opt_level"):
            self.assertEqual(candidate["build"][key],baseline["build"][key])
        self.assertNotEqual(candidate["build"]["rust_driver_libraries_sha256"],
                            baseline["build"]["rust_driver_libraries_sha256"])

    def test_mixed_mul_device_evidence_keeps_kernel_c_std_and_vectors_fixed(self):
        root = Path(__file__).resolve().parents[2]
        reports = root / "tests/arithmetic-parity/results"
        baseline = json.loads((reports / "esp32s3-constant-hwloop-math-2026-10-06.json").read_text())
        candidate = json.loads((reports / "esp32s3-mixed-mul-math-2026-10-06.json").read_text())
        compiler = json.loads((reports / "compiler-mixed-mul-2026-10-06.json").read_text())
        self.check_control_inputs(baseline, candidate)
        self.assertEqual(candidate["build"]["kernel_libraries_inventory_sha256"],
                         baseline["build"]["kernel_libraries_inventory_sha256"])
        self.assertEqual(candidate["build"]["std_proposal_ledger"],
                         baseline["build"]["std_proposal_ledger"])
        self.assertEqual([row["function_bytes"]["c"] for row in candidate["cases"]],
                         [row["function_bytes"]["c"] for row in baseline["cases"]])
        self.assertEqual(candidate["build"]["compiler_package_provenance_sha256"],
                         compiler["compiler_package_provenance_sha256"])
        self.assertEqual(candidate["build"]["rust_driver_libraries_sha256"],
                         {"lib/" + name: sha for name, sha in compiler["rustc_driver_library_sha256"].items()})
        ledger = candidate["build"]["patch_ledger"]
        self.assertEqual(ledger["proposal_set"], "mixed-mul")
        self.assertEqual(ledger["qualification_tests"], 114)
        self.assertEqual(len(ledger["patches"]), 18)
        self.assertEqual(ledger["patches"][:16], baseline["build"]["patch_ledger"]["patches"])
        self.assertEqual([{key: row[key] for key in ("name", "sha256")} for row in ledger["patches"]],
                         compiler["patches"])
        self.assertEqual(ledger["extra_qualification"]["test_suites"][1]["expected_passes"], 5)
        self.check_patch_chain(root, ledger)

    def test_zero_compare_device_evidence_keeps_controls_and_binds_real_driver(self):
        root = Path(__file__).resolve().parents[2]
        reports = root / "tests/arithmetic-parity/results"
        compiler = json.loads((reports / "compiler-zero-compare-2026-10-06.json").read_text())
        cohorts = (
            ("esp32s3-mixed-mul-math-2026-10-06.json",
             "esp32s3-zero-compare-math-2026-10-06.json", 1136, 942),
            ("esp32s3-mul-width-control-2026-10-06.json",
             "esp32s3-zero-compare-widths-2026-10-06.json", 8, 8),
        )
        self.check_device_compiler_cohorts(root, reports, compiler, cohorts,
                                          "zero-compare", 18, 19, 115, 414)

    def test_signbits_device_evidence_keeps_controls_and_binds_real_driver(self):
        root = Path(__file__).resolve().parents[2]
        reports = root / "tests/arithmetic-parity/results"
        compiler = json.loads((reports / "compiler-signbits-2026-10-06.json").read_text())
        cohorts = (
            ("esp32s3-zero-compare-math-2026-10-06.json",
             "esp32s3-signbits-math-2026-10-06.json", 1136, 942),
            ("esp32s3-zero-compare-widths-2026-10-06.json",
             "esp32s3-signbits-widths-2026-10-06.json", 8, 8),
        )
        self.check_device_compiler_cohorts(root, reports, compiler, cohorts,
                                          "signed-overflow", 19, 20, 116, 415)

    def check_device_compiler_cohorts(self, root, reports, compiler, cohorts,
                                     selection, parent_count, patch_count,
                                     qualification_tests, optimizer_passes):
        for baseline_name, candidate_name, cases, c_cases in cohorts:
            with self.subTest(report=candidate_name):
                self.check_published_evidence(reports / candidate_name, cases, c_cases)
                baseline = json.loads((reports / baseline_name).read_text())
                candidate = json.loads((reports / candidate_name).read_text())
                self.check_compiler_followup(root, baseline, candidate, selection,
                                             parent_count, patch_count,
                                             qualification_tests, optimizer_passes)
                self.assertEqual(candidate["build"]["std_proposal_ledger"],
                                 baseline["build"]["std_proposal_ledger"])
                self.assertEqual([row["function_bytes"]["c"] for row in candidate["cases"]],
                                 [row["function_bytes"]["c"] for row in baseline["cases"]])
                self.assertEqual(candidate["build"]["compiler_package_provenance_sha256"],
                                 compiler["compiler_package_provenance_sha256"])
                self.assertEqual(candidate["build"]["rust_driver_libraries_sha256"],
                                 {"lib/" + name: sha for name, sha in
                                  compiler["driver_libraries_sha256"].items()})
                ledger = candidate["build"]["patch_ledger"]
                self.assertEqual([{key: row[key] for key in ("name", "sha256")}
                                  for row in ledger["patches"]], compiler["patches"])

    def test_mul_width_device_evidence_binds_stratification_and_qualified_driver(self):
        root = Path(__file__).resolve().parents[2]
        reports = root / "tests/arithmetic-parity/results"
        controls = []
        for label in ("control", "candidate"):
            path = reports / f"esp32s3-mul-width-{label}-2026-10-06.json"
            self.check_published_evidence(path, case_count=8, c_case_count=8)
            evidence = json.loads(path.read_text())
            self.assertEqual([row["name"] for row in evidence["cases"]],
                             [row["name"] for row in mul_widths.catalog()])
            self.assertEqual(evidence["samples_per_case"], 64)
            self.assertEqual(evidence["repeats_per_mode_per_run"], 5)
            self.assertEqual(evidence["totals"]["raw_pair_differences"], 0)
            self.assertTrue(all(not values for row in evidence["cases"]
                                for failures in row["failures"]
                                for values in failures.values()))
            controls.append(evidence)
        baseline, candidate = controls
        self.check_compiler_followup(root, baseline, candidate, "mul-width",
                                     parent_patch_count=18, patch_count=19,
                                     qualification_tests=115, optimizer_passes=414)
        before, after = baseline["build"], candidate["build"]
        self.assertEqual(after["std_proposal_ledger"], before["std_proposal_ledger"])
        self.assertEqual([row["function_bytes"]["c"] for row in candidate["cases"]],
                         [row["function_bytes"]["c"] for row in baseline["cases"]])
        compiler = json.loads((reports / "compiler-mul-width-2026-10-06.json").read_text())
        self.assertEqual(after["compiler_package_provenance_sha256"],
                         compiler["compiler_package_provenance_sha256"])
        self.assertEqual(after["rust_driver_libraries_sha256"],
                         {"lib/" + name: sha for name, sha in compiler["driver_libraries_sha256"].items()})
        self.assertEqual([{key: row[key] for key in ("name", "sha256")}
                          for row in after["patch_ledger"]["patches"]], compiler["patches"])

    def check_patch_chain(self, root, ledger):
        manifest=json.loads((root/"upstream/rust-llvm/upstream.json").read_text())
        self.assertEqual(ledger["upstream_revision"],manifest["revision"])
        self.assertEqual(ledger["rust_revision"],manifest["rust_revision"])
        for row in ledger["patches"]:
            folder="proposals" if row.get("proposal") else "patches"
            self.assertEqual(build.digest(root/"upstream/rust-llvm"/folder/row["name"]),row["sha256"])

    def fixture(self, root):
        generated, linked, captured = (root/name for name in ("generated","linked","captured"))
        generate.generate(generated,False,["u32_wrapping_add"])
        coverage=json.loads((generated/"coverage.json").read_text())
        linked.mkdir();captured.mkdir()
        for name in ("app.elf","image.bin","resolved.config"):
            (linked/name).write_bytes(name.encode())
        artifacts={name:build.digest(linked/name) for name in ("app.elf","image.bin","resolved.config")}
        sha=build.digest(generated/"coverage.json")
        provenance=dict(artifacts=artifacts,coverage_sha256=sha,kernel_config_sha256="config",
                        c_compiler_version="test compiler",c_compiler_sha256="compiler",c_flags="O2",
                        size_scope="function-only",symbols={"aq_c_0000":{"function_bytes":20},
                                                           "aq_r_0000":{"function_bytes":24}},
                        compiler_input=dict(compiler_sha256="compiler",compiler_libraries_sha256={},
                                            compiler_package_provenance_sha256="package",target_sha256="target",
                                            std_inventory_sha256="std",std_features=[],application_opt_level="2",
                                            patch_ledger={}))
        (linked/"build-provenance.json").write_text(json.dumps(provenance))
        raw=capture(coverage)
        (captured/"run-001.raw").write_bytes(raw)
        (captured/"serial.raw").write_bytes(raw)
        parsed=measure.parse(raw,coverage)
        parsed.update(raw_capture=1,raw_bytes=len(raw),raw_sha256=hashlib.sha256(raw).hexdigest(),aggregate_offset=0)
        summary=dict(failure=None,restoration={"verified":True},image_sha256=artifacts["image.bin"],
                     coverage_sha256=sha,aggregate_raw_sha256=hashlib.sha256(raw).hexdigest(),runs=[parsed])
        (captured/"summary.json").write_text(json.dumps(summary))
        return generated,linked,captured

    def test_public_summary_preserves_all_cycles_and_excludes_private_paths(self):
        with tempfile.TemporaryDirectory(prefix="aq-report-") as folder:
            paths=self.fixture(Path(folder))
            result=report.summarize(*paths)
            self.assertTrue(result["passed"])
            self.assertEqual(result["cases"][0]["cycles"]["masked"]["c"],[[11]*5])
            self.assertEqual(result["cases"][0]["function_bytes"],{"c":20,"rust":24})
            self.assertNotIn(folder,json.dumps(result))

    def test_saved_results_are_not_trusted_without_reparsing(self):
        with tempfile.TemporaryDirectory(prefix="aq-report-") as folder:
            paths=self.fixture(Path(folder));summary=paths[2]/"summary.json"
            value=json.loads(summary.read_text())
            value["runs"][0]["cases"][0]["timings"]["masked"][0]["c_cycles"]=12
            summary.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError,"reparsed capture"):
                report.summarize(*paths)

    def test_optional_std_patch_provenance_is_retained_separately(self):
        with tempfile.TemporaryDirectory(prefix="aq-std-report-") as folder:
            paths = self.fixture(Path(folder))
            proof = paths[1] / "build-provenance.json"
            value = json.loads(proof.read_text())
            ledger = {"proposal": "fixture", "files": {"std/src/num/f32.rs": {}}}
            value["compiler_input"]["std_proposal_ledger"] = ledger
            proof.write_text(json.dumps(value))
            result = report.summarize(*paths)
            self.assertEqual(result["build"]["std_proposal_ledger"], ledger)
            self.assertEqual(result["build"]["patch_ledger"], {})

    def test_kernel_control_digest_is_canonical_and_path_free(self):
        with tempfile.TemporaryDirectory(prefix="aq-kernel-report-") as folder:
            paths = self.fixture(Path(folder))
            proof = paths[1] / "build-provenance.json"
            value = json.loads(proof.read_text())
            libraries = {"libsched.a": "b" * 64, "libc.a": {"member.o": "a" * 64}}
            value["kernel_libraries_sha256"] = libraries
            proof.write_text(json.dumps(value))
            result = report.summarize(*paths)
            expected = hashlib.sha256(json.dumps(
                libraries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            self.assertEqual(result["build"]["kernel_libraries_inventory_sha256"], expected)

    def test_unverified_restore_or_changed_image_blocks_export(self):
        with tempfile.TemporaryDirectory(prefix="aq-report-") as folder:
            paths=self.fixture(Path(folder));summary=paths[2]/"summary.json"
            value=json.loads(summary.read_text());value["restoration"]["verified"]=False
            summary.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError,"restoration"):
                report.summarize(*paths)
            value["restoration"]["verified"]=True;summary.write_text(json.dumps(value))
            (paths[1]/"image.bin").write_bytes(b"different")
            with self.assertRaisesRegex(ValueError,"artifact changed"):
                report.summarize(*paths)

    def test_ansi_recovery_is_explicit_and_preserves_original_failure(self):
        with tempfile.TemporaryDirectory(prefix="aq-report-") as folder:
            paths=self.fixture(Path(folder));captured=paths[2]
            summary=captured/"summary.json";saved=json.loads(summary.read_text())
            raw=(captured/"run-001.raw").read_bytes().replace(b"nsh> ",b"nsh> \x1b[K")
            (captured/"run-001.raw").write_bytes(raw);(captured/"serial.raw").write_bytes(raw)
            saved.update(failure="ValueError",runs=[],aggregate_raw_sha256=hashlib.sha256(raw).hexdigest())
            original=json.dumps(saved);summary.write_text(original)
            proof=dict(failure_detail="ValueError: final NSH prompt must follow AQ_DONE",restoration={"verified":True})
            (captured/"proof.json").write_text(json.dumps(proof))
            with self.assertRaisesRegex(ValueError,"incomplete measurement"):
                report.summarize(*paths)
            result=report.summarize(*paths,recover_ansi_prompt=True)
            self.assertTrue(result["passed"])
            self.assertTrue(result["collection"][0]["recovered_ansi_prompt"])
            self.assertEqual(summary.read_text(),original)
            proof["failure_detail"]="another parser error"
            (captured/"proof.json").write_text(json.dumps(proof))
            with self.assertRaisesRegex(ValueError,"not the recoverable"):
                report.summarize(*paths,recover_ansi_prompt=True)


if __name__=="__main__":unittest.main()
