"""Authenticate the clean build shared by seven distinct primitive captures."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import sys
import unittest
from unittest import mock

import test_analyze as channel_tests
import test_mutex_analyze as primitive_tests

analyzer = channel_tests.analyze
with mock.patch.dict(sys.modules, {"analyze": analyzer}):
    capture = channel_tests.module("suite_binding_capture_tests", channel_tests.HERE / "capture.py")


class SuiteBindingTests(unittest.TestCase):
    def setUp(self):
        # Reuse authenticated synthetic archive construction, not a mock of
        # build_receipt/bind_build. These bytes make no target execution claim.
        self.build = channel_tests.BuildEvidenceTests()
        self.addCleanup(self.build.doCleanups)
        self.build.setUp()

    def prepare_clean_suite(self):
        root = self.build.root
        shutil.rmtree(root / "crossbeam")
        source = self.build.prep.prepare(root / "crossbeam-channel.crate",
                                         root / "crossbeam-utils.crate",
                                         root / "crossbeam", "clean")
        self.build.receipt.update(mode="std-mutex-suite", hook_stage=0,
                                  crossbeam_receipt=source)
        library = analyzer.read_json(root / "prepared/std-library-sources.json")
        library.update(analyzer.std_transfer_validator().STD_SOURCE_SHA256)
        self.reseal_std_library(library)

    def reseal_std_library(self, library):
        # Recompute every outer digest as a forged-but-self-consistent ledger
        # could. The independently pinned primitive source must still match.
        hook = analyzer.read_json(self.build.root / "prepared/std-channel-hook.json")
        hook["library_tree_sha256"] = analyzer.canonical(library)
        self.build.receipt["std_library_tree_sha256"] = hook["library_tree_sha256"]
        self.build.write("prepared/std-library-sources.json", library)
        self.build.write("prepared/std-channel-hook.json", hook)
        self.build.write("firmware/std-patch.json", {"channel_qualification": hook})
        self.build.refresh()

    def summary(self, mode):
        metadata, result = primitive_tests.fixture(mode)
        metadata["build_id"] = self.build.receipt["build_id"]
        return analyzer.analyze(channel_tests.transcript(metadata, result))

    def test_clean_image_authenticates_all_seven_distinct_case_contracts(self):
        self.prepare_clean_suite()
        identities = set()
        for mode, (outcome, counts) in primitive_tests.CASES.items():
            with self.subTest(mode=mode):
                receipt = analyzer.build_receipt(self.build.root, mode)
                bound = analyzer.bind_build(self.summary(mode), receipt)
                analyzer.require_no_repair(bound)
                identities.add(receipt["build_id"])
                self.assertEqual(bound["mode"], mode)
                self.assertEqual(bound["build"]["mode"], "std-mutex-suite")
                self.assertEqual(bound["outcome"], outcome)
                self.assertEqual(bound["result"]["accepted"], counts[1])
                self.assertEqual(bound["result"]["transferred"], counts[2])
                self.assertEqual(bound["scope"], "std-transfer-primitives")
                self.assertTrue(bound["build_bound"])
                for claim in ("physical_execution_verified", "priority_progress_qualified",
                              "production_transport_qualified", "timing_qualified"):
                    self.assertFalse(bound[claim])
        self.assertEqual(len(identities), 1)

    def test_instrumented_channel_build_cannot_bind_a_primitive_result(self):
        receipt = analyzer.build_receipt(self.build.root, self.build.mode)
        with self.assertRaises(ValueError):
            analyzer.build_receipt(self.build.root, "mutex-admit-held")
        with self.assertRaisesRegex(ValueError, "identities differ"):
            analyzer.bind_build(self.summary("mutex-admit-held"), receipt)

    def test_clean_primitive_build_cannot_bind_a_channel_result(self):
        self.prepare_clean_suite()
        receipt = analyzer.build_receipt(self.build.root, "std-mutex-suite")
        for mode in analyzer.CHANNEL_MODES - {"host-functional"}:
            with self.subTest(mode=mode):
                metadata, result = channel_tests.fixture(mode, stalled=False)
                metadata["build_id"] = receipt["build_id"]
                summary = analyzer.analyze(channel_tests.transcript(metadata, result))
                with self.assertRaises(ValueError):
                    analyzer.build_receipt(self.build.root, mode)
                with self.assertRaisesRegex(ValueError, "identities differ"):
                    analyzer.bind_build(summary, receipt)

    def test_runtime_build_identity_must_match_the_clean_image(self):
        self.prepare_clean_suite()
        mode = "condvar-close-wait"
        summary = self.summary(mode)
        summary["metadata"]["build_id"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "identities differ"):
            analyzer.bind_build(summary, analyzer.build_receipt(self.build.root, mode))

    def test_missing_or_changed_std_primitives_cannot_be_resealed_into_acceptance(self):
        self.prepare_clean_suite()
        library = analyzer.read_json(self.build.root / "prepared/std-library-sources.json")
        for path in analyzer.std_transfer_validator().STD_SOURCE_SHA256:
            for change in ("missing", "changed"):
                with self.subTest(path=path, change=change):
                    modified = dict(library)
                    if change == "missing":
                        del modified[path]
                    else:
                        modified[path] = "0" * 64
                    self.reseal_std_library(modified)
                    with self.assertRaises(ValueError):
                        analyzer.build_receipt(self.build.root, "condvar-wait-publish")
        self.reseal_std_library(library)
        analyzer.build_receipt(self.build.root, "condvar-wait-publish")

    def test_sdk_relabelling_cannot_admit_a_different_dispatch_variant(self):
        self.prepare_clean_suite()
        for sdk in ("esp-1.90.0.0", "unqualified-sdk"):
            with self.subTest(sdk=sdk):
                self.build.receipt["sdk"] = sdk
                self.build.refresh()
                with self.assertRaises(ValueError):
                    analyzer.build_receipt(self.build.root, "std-mutex-suite")

    def test_suite_label_is_not_an_executable_case(self):
        self.prepare_clean_suite()
        metadata, result = primitive_tests.fixture()
        for record in (metadata, result):
            record["mode"] = "std-mutex-suite"
        with self.assertRaises(ValueError):
            analyzer.analyze(channel_tests.transcript(metadata, result))
        with self.assertRaises(ValueError):
            analyzer.build_receipt(self.build.root, "unknown-suite")

    def test_dispatch_preserves_strict_json_and_same_schema_record_pair(self):
        metadata, result = primitive_tests.fixture("condvar-wait-publish")
        text = channel_tests.transcript(metadata, result)
        for broken in (
            text.replace('"schema": "std-transfer-progress-v1"',
                         '"schema": "channel-progress-v1"', 1),
            text.replace('"schema": "std-transfer-progress-v1"',
                         '"schema": "unknown"', 1),
            text.replace('"schema":', '"schema":"duplicate", "schema":', 1),
            text.replace('"capacity": 1', '"capacity": NaN', 1),
            "\n".join(text.splitlines()[:-1]),
            text + text,
            text + "HardFault\n",
        ):
            with self.subTest(broken=broken[:160]), self.assertRaises(ValueError):
                analyzer.analyze(broken)

    def test_cli_records_busy_acceptance_without_claiming_transfer(self):
        self.prepare_clean_suite()
        metadata, result = primitive_tests.fixture("mutex-admit-held")
        metadata["build_id"] = self.build.receipt["build_id"]
        console = self.build.root / "console.log"
        output = self.build.root / "acceptance.json"
        console.write_text(channel_tests.transcript(metadata, result))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = analyzer.main([str(console), "--build-receipt", str(self.build.root),
                                  "--output", str(output), "--require-no-repair"])
        self.assertEqual(code, 0)
        summary = json.loads(output.read_text())
        self.assertEqual(summary["outcome"], "busy")
        self.assertEqual(summary["result"]["accepted"], 0)
        self.assertEqual(summary["result"]["transferred"], 0)
        self.assertTrue(summary["primitive_contract_satisfied"])
        self.assertFalse(summary["production_transport_qualified"])

    def test_capture_refuses_another_valid_case_from_the_same_image(self):
        self.prepare_clean_suite()
        args = argparse.Namespace(
            build_directory=self.build.root, mode="mutex-admit-held",
            output=self.build.root / "capture", runtime="qemu", timeout=1,
            qemu="unused-synthetic-qemu", session=None, port=None,
        )
        metadata, result = primitive_tests.fixture("mutex-take-held")
        metadata["build_id"] = self.build.receipt["build_id"]
        responses = iter((b"*** Booting NuttX ***\r\nnsh> ",
                          b"channel_progress mutex-admit-held\r\n" +
                          channel_tests.transcript(metadata, result).encode() + b"nsh> "))

        def read_until(fd, transcript, predicate, deadline, process):
            data = next(responses)
            self.assertTrue(predicate(data))
            transcript.extend(data)
            return data

        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        with mock.patch.object(capture.subprocess, "Popen", return_value=process), \
                mock.patch.object(capture, "read_until", side_effect=read_until), \
                mock.patch.object(capture, "write_command"):
            report = capture.capture(args)
        self.assertFalse(report["capture_complete"])
        self.assertEqual(report["failure_stage"], "analysis")
        self.assertIn("runtime case differs", report["error"])
        self.assertIn(b"mutex-take-held", (args.output / "console.log").read_bytes())
        self.assertTrue((args.output / "capture.json").is_file())
        self.assertFalse((args.output / "analysis.json").exists())
        process.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
