"""Authenticate the one-image FIFO suite without substituting target evidence."""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import shutil
import sys
import unittest
from unittest import mock

import test_analyze as channel_tests

analyzer = channel_tests.analyze
with mock.patch.dict(sys.modules, {"analyze": analyzer}):
    capture = channel_tests.module("fifo_binding_capture_tests", channel_tests.HERE / "capture.py")


class FifoBindingTests(unittest.TestCase):
    def setUp(self):
        self.build = channel_tests.BuildEvidenceTests()
        self.addCleanup(self.build.doCleanups)
        self.build.setUp()
        self.kernel = analyzer.kernel_source_validator()
        self.kernel_bytes = {
            path: ("// synthetic unit fixture, never kernel execution: " + path + "\n").encode()
            for path in self.kernel.KERNEL_SOURCE_SHA256
        }
        self.kernel_pins = {
            path: analyzer.hashlib.sha256(data).hexdigest()
            for path, data in self.kernel_bytes.items()
        }
        # Only immutable source pins are replaced for synthetic input bytes.
        # Actual source preparation, path validation, receipt reconstruction,
        # build identity and retained-file checks all execute unmocked.
        for patch in (mock.patch.object(self.kernel, "KERNEL_SOURCE_SHA256", self.kernel_pins),
                      mock.patch.object(analyzer, "kernel_source_validator", return_value=self.kernel)):
            patch.start()
            self.addCleanup(patch.stop)

    def prepare_fifo_suite(self):
        root = self.build.root
        shutil.rmtree(root / "crossbeam")
        source = self.build.prep.prepare(root / "crossbeam-channel.crate",
                                         root / "crossbeam-utils.crate",
                                         root / "crossbeam", "fifo-ordered")
        kernel_root = root / "synthetic-kernel"
        for name, data in self.kernel_bytes.items():
            path = kernel_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        retained = self.kernel.prepare_kernel(kernel_root, root / "prepared/fifo-kernel")
        self.build.receipt.update(
            mode="fifo-channel-suite", hook_stage=5, crossbeam_receipt=source,
            kernel_revision=self.kernel.KERNEL_REVISION,
            fifo_kernel_source_sha256=retained,
            fifo_kernel_source_proof="firmware/fifo-kernel-source.json",
        )
        self.build.write("firmware/fifo-kernel-source.json", {
            "schema": "fifo-kernel-source-v1", "kernel_revision": self.kernel.KERNEL_REVISION,
            "source_sha256": retained, "selected_source_matches": True,
            "source_root": str(root / "firmware/nuttx"),
            "expected_source_root": str(root / "firmware/nuttx"),
        })
        library = analyzer.read_json(root / "prepared/std-library-sources.json")
        library.update(analyzer.std_transfer_validator().STD_SOURCE_SHA256)
        library.update(self.kernel.STD_SOURCE_SHA256)
        self.reseal_std_library(library)

    def reseal_std_library(self, library):
        hook = analyzer.read_json(self.build.root / "prepared/std-channel-hook.json")
        hook["library_tree_sha256"] = analyzer.canonical(library)
        self.build.receipt["std_library_tree_sha256"] = hook["library_tree_sha256"]
        self.build.write("prepared/std-library-sources.json", library)
        self.build.write("prepared/std-channel-hook.json", hook)
        self.build.write("firmware/std-patch.json", {"channel_qualification": hook})
        self.build.refresh()

    def summary(self, mode):
        import test_fifo_analyze as fifo_tests
        metadata, result = fifo_tests.fixture(mode)
        metadata["build_id"] = self.build.receipt["build_id"]
        return analyzer.analyze(channel_tests.transcript(metadata, result))

    def test_six_distinct_cases_authenticate_one_frozen_image(self):
        self.prepare_fifo_suite()
        identities = set()
        for mode in analyzer.FIFO_MODES:
            with self.subTest(mode=mode):
                receipt = analyzer.build_receipt(self.build.root, mode)
                bound = analyzer.bind_build(self.summary(mode), receipt)
                analyzer.require_no_repair(bound)
                identities.add((receipt["build_id"], receipt["image_sha256"]))
                self.assertEqual(bound["mode"], mode)
                self.assertEqual(bound["scope"], "equal-fifo-reservation-progress")
                self.assertEqual(receipt["mode"], "fifo-channel-suite")
                self.assertTrue(bound["build_bound"])
                for claim in ("physical_execution_verified", "priority_progress_qualified",
                              "production_transport_qualified", "timing_qualified"):
                    self.assertFalse(bound[claim])
        self.assertEqual(len(identities), 1)

    def test_old_build_and_other_suite_cannot_bind_fifo_cases(self):
        old_receipt = analyzer.build_receipt(self.build.root, self.build.mode)
        with self.assertRaises(ValueError):
            analyzer.build_receipt(self.build.root, analyzer.FIFO_MODES[0])
        with self.assertRaisesRegex(ValueError, "identities differ"):
            analyzer.bind_build(self.summary(analyzer.FIFO_MODES[0]), old_receipt)
        self.prepare_fifo_suite()
        for mode in (*analyzer.CHANNEL_MODES, *analyzer.STD_TRANSFER_MODES):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                analyzer.build_receipt(self.build.root, mode)

    def test_kernel_inventory_is_a_frozen_build_identity_input(self):
        self.prepare_fifo_suite()
        path = next(iter(self.kernel_pins))
        self.build.receipt["fifo_kernel_source_sha256"][path] = "0" * 64
        self.build.write("build.json", self.build.receipt)
        with self.assertRaisesRegex(ValueError, "frozen inputs"):
            analyzer.build_receipt(self.build.root, "fifo-channel-suite")

    def test_changed_kernel_bytes_cannot_be_resealed_into_acceptance(self):
        self.prepare_fifo_suite()
        for name, data in self.kernel_bytes.items():
            path = self.build.root / "prepared/fifo-kernel" / name
            with self.subTest(name=name):
                path.write_bytes(data + b"changed source")
                self.build.receipt["fifo_kernel_source_sha256"][name] = analyzer.sha256(path)
                proof = analyzer.read_json(self.build.root / "firmware/fifo-kernel-source.json")
                proof["source_sha256"] = copy.deepcopy(self.build.receipt["fifo_kernel_source_sha256"])
                self.build.write("firmware/fifo-kernel-source.json", proof)
                self.build.refresh()
                with self.assertRaises(ValueError):
                    analyzer.build_receipt(self.build.root, "fifo-channel-suite")
                path.write_bytes(data)
                self.build.receipt["fifo_kernel_source_sha256"][name] = self.kernel_pins[name]

    def test_revision_compiled_path_and_actual_compilation_check_are_required(self):
        self.prepare_fifo_suite()
        proof = analyzer.read_json(self.build.root / "firmware/fifo-kernel-source.json")
        for key, value in (
            ("kernel_revision", "0" * 40), ("selected_source_matches", False),
            ("source_root", "/another/kernel"), ("expected_source_root", "/another/kernel"),
            ("source_sha256", {}),
        ):
            with self.subTest(key=key):
                self.build.write("firmware/fifo-kernel-source.json", {**proof, key: value})
                self.build.refresh()
                with self.assertRaises(ValueError):
                    analyzer.build_receipt(self.build.root, "fifo-channel-suite")
        self.build.write("firmware/fifo-kernel-source.json", proof)
        self.build.receipt["kernel_revision"] = "0" * 40
        self.build.refresh()
        with self.assertRaisesRegex(ValueError, "kernel revision"):
            analyzer.build_receipt(self.build.root, "fifo-channel-suite")

    def test_raw_kernel_copies_are_required_after_build_relocation(self):
        self.prepare_fifo_suite()
        relocated = self.build.root / "relocated"
        shutil.copytree(self.build.root, relocated,
                        ignore=shutil.ignore_patterns("relocated", "sources", "synthetic-kernel"))
        actual = analyzer.build_receipt(relocated, "fifo-channel-suite")
        self.assertEqual(actual["build_id"], self.build.receipt["build_id"])
        name = next(iter(self.kernel_pins))
        (relocated / "prepared/fifo-kernel" / name).unlink()
        with self.assertRaises(ValueError):
            analyzer.build_receipt(relocated, "fifo-channel-suite")

    def test_prepared_parker_and_sdk_dispatch_are_independently_pinned(self):
        self.prepare_fifo_suite()
        library = analyzer.read_json(self.build.root / "prepared/std-library-sources.json")
        for name in (*self.kernel.STD_SOURCE_SHA256,
                     *analyzer.std_transfer_validator().STD_SOURCE_SHA256):
            with self.subTest(name=name):
                changed = {**library, name: "0" * 64}
                self.reseal_std_library(changed)
                with self.assertRaises(ValueError):
                    analyzer.build_receipt(self.build.root, "fifo-channel-suite")
        self.reseal_std_library(library)
        self.build.receipt["sdk"] = "esp-1.90.0.0"
        self.build.refresh()
        with self.assertRaises(ValueError):
            analyzer.build_receipt(self.build.root, "fifo-channel-suite")

    def test_suite_label_cannot_execute_and_mixed_record_schemas_are_rejected(self):
        import test_fifo_analyze as fifo_tests
        metadata, result = fifo_tests.fixture(analyzer.FIFO_MODES[0])
        text = channel_tests.transcript(metadata, result)
        for broken in (
            text.replace('"mode": "fifo-try-recv-reserved"', '"mode": "fifo-channel-suite"'),
            text.replace('"schema": "fifo-channel-progress-v1"',
                         '"schema": "channel-progress-v1"', 1),
            text.replace('"schema":', '"schema":"duplicate", "schema":', 1),
            text + text, text + "HardFault\n",
        ):
            with self.subTest(broken=broken[:120]), self.assertRaises(ValueError):
                analyzer.analyze(broken)
        with self.assertRaises(ValueError):
            capture.command_for("fifo-channel-suite")

    def test_cli_pass_retains_narrow_scope_and_cannot_self_certify_physical_execution(self):
        self.prepare_fifo_suite()
        summary = self.summary("fifo-select-reserved")
        console = self.build.root / "console.log"
        output = self.build.root / "acceptance.json"
        console.write_text(channel_tests.transcript(summary["metadata"], summary["result"]))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = analyzer.main([str(console), "--build-receipt", str(self.build.root),
                                  "--output", str(output), "--require-no-repair"])
        self.assertEqual(code, 0)
        actual = json.loads(output.read_text())
        self.assertTrue(actual["completed_without_repair"])
        self.assertFalse(actual["production_transport_qualified"])
        self.assertFalse(actual["physical_execution_verified"])
        session = dict(schema="channel-progress-session-v1", runtime_kind="physical",
                       board="operator-label", measurement_session="synthetic",
                       board_serial="synthetic", flash_verification="synthetic", active_cores=1,
                       build_id=self.build.receipt["build_id"],
                       image_sha256=self.build.receipt["image_sha256"])
        relabelled = analyzer.attach_execution(actual, session, True)
        self.assertFalse(relabelled["physical_execution_verified"])
        self.assertEqual(relabelled["execution_evidence"], "operator-asserted")

    def test_capture_rejects_another_valid_fifo_case_from_the_same_image(self):
        self.prepare_fifo_suite()
        args = argparse.Namespace(build_directory=self.build.root, mode="fifo-try-recv-reserved",
                                  output=self.build.root / "capture", runtime="qemu", timeout=1,
                                  qemu="unused-synthetic-qemu", session=None, port=None)
        summary = self.summary("fifo-try-recv-published")
        responses = iter((b"*** Booting NuttX ***\r\nnsh> ",
                          b"channel_progress fifo-try-recv-reserved\r\n" +
                          channel_tests.transcript(summary["metadata"], summary["result"]).encode() + b"nsh> "))

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
        self.assertTrue((args.output / "console.log").is_file())
        self.assertTrue((args.output / "capture.json").is_file())
        self.assertFalse((args.output / "analysis.json").exists())


if __name__ == "__main__":
    unittest.main()
