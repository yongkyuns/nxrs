"""Host-only parser and restoration tests for the arithmetic runner."""
import json
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import measure


def manifest(count=1):
    return {"schema": 1, "samples_per_case": 64, "cases": [
        {"id": i, "name": f"case-{i}", "kind": "integer", "c_available": i == 0,
         "expected": [[7, 0, 0, 0] if j == 0 else [0, 0, 0, 0]
                      for j in range(64)]}
        for i in range(count)
    ]}


def capture(coverage, *, errors=0, omit_case=None, omit_time=None, done=True,
            bad_expected=False, count=64):
    rows = []
    total = 0
    for case in coverage["cases"]:
        ident = case["id"]
        if ident == omit_case:
            continue
        total += count
        c_present = int(case["c_available"])
        c_errors = errors if ident == 0 else 0
        rows.append(f"AQ_CASE id={ident} count={count} c_present={c_present} "
                    f"c_errors={c_errors} rust_errors=0 pair_diff=0 "
                    "c_max_ulp=0 rust_max_ulp=0")
        if c_errors:
            expected = 8 if bad_expected else 7
            rows.append(f"AQ_FAIL backend=c id={ident} sample=0 "
                        "actual_lo=0x0000000000000063 actual_hi=0x0000000000000000 "
                        f"actual_flag=0 expected_lo=0x{expected:016x} "
                        "expected_hi=0x0000000000000000 expected_flag=0")
        for mode in ("masked", "normal"):
            for repeat in range(5):
                if (ident, mode, repeat) == omit_time:
                    continue
                cc = 0 if not c_present else 11
                rows.append(f"AQ_TIME id={ident} mode={mode} repeat={repeat} "
                            f"c_cycles={cc} rust_cycles=17")
    if done:
        rows.append(f"AQ_DONE cases={len(coverage['cases'])} samples={total} "
                    f"c_errors={errors} rust_errors=0 repeats=5")
    rows.append("nsh> ")
    return ("\n".join(rows) + "\n").encode()


class MeasureParserTests(unittest.TestCase):
    def test_nsh_ansi_prompt_does_not_reject_complete_device_output(self):
        coverage=manifest()
        raw=capture(coverage).replace(b"nsh> ", b"nsh> \x1b[K")
        self.assertTrue(measure.parse(raw,coverage)["passed"])

    def test_valid_capture_has_two_five_repeat_timing_arrays(self):
        result = measure.parse(capture(manifest()), manifest())
        self.assertTrue(result["passed"])
        self.assertEqual(set(result["cases"][0]["timings"]), {"masked", "normal"})
        self.assertEqual([x["repeat"] for x in result["cases"][0]["timings"]["masked"]],
                         list(range(5)))

    def test_correctness_failure_is_preserved_as_failed_result(self):
        coverage = manifest()
        result = measure.parse(capture(coverage, errors=1), coverage)
        self.assertFalse(result["passed"])
        self.assertEqual(result["cases"][0]["c_errors"], 1)
        self.assertEqual(result["cases"][0]["failures"]["c"][0]["actual_lo"], 99)

    def test_rejects_duplicate_case_and_missing_case_rows(self):
        coverage = manifest(2)
        raw = capture(coverage)
        with self.assertRaisesRegex(ValueError, "missing"):
            measure.parse(capture(coverage, omit_case=1), coverage)
        duplicate = raw.replace(b"AQ_CASE id=1", b"AQ_CASE id=0", 1)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            measure.parse(duplicate, coverage)

    def test_rejects_bad_counts_duplicate_or_missing_timing_and_completion(self):
        coverage = manifest()
        with self.assertRaisesRegex(ValueError, "totals"):
            measure.parse(capture(coverage).replace(b"samples=64", b"samples=65"), coverage)
        with self.assertRaisesRegex(ValueError, "timing rows missing"):
            measure.parse(capture(coverage, omit_time=(0, "normal", 4)), coverage)
        with self.assertRaisesRegex(ValueError, "AQ_DONE"):
            measure.parse(capture(coverage, done=False), coverage)
        too_many = capture(coverage).replace(b"count=64", b"count=65", 1)
        with self.assertRaisesRegex(ValueError, "count/presence"):
            measure.parse(too_many, coverage)
        too_few = capture(coverage).replace(b"count=64", b"count=63", 1)
        with self.assertRaisesRegex(ValueError, "count/presence"):
            measure.parse(too_few, coverage)

    def test_rejects_duplicate_timing_and_malformed_diagnostic_rows(self):
        coverage = manifest()
        raw = capture(coverage)
        timing = next(line for line in raw.splitlines() if line.startswith(b"AQ_TIME"))
        duplicate = raw.replace(timing + b"\n", timing + b"\n" + timing + b"\n", 1)
        with self.assertRaisesRegex(ValueError, "duplicate AQ_TIME"):
            measure.parse(duplicate, coverage)
        malformed = raw.replace(b"AQ_TIME id=0", b"AQ_TIME nope", 1)
        with self.assertRaisesRegex(ValueError, "malformed AQ_TIME"):
            measure.parse(malformed, coverage)

    def test_requires_c_cycles_and_zero_pair_diff_when_c_is_absent(self):
        no_c = manifest(2)
        raw = capture(no_c).replace(b"id=1 count=64 c_present=0 c_errors=0 "
                                    b"rust_errors=0 pair_diff=0",
                                    b"id=1 count=64 c_present=0 c_errors=0 "
                                    b"rust_errors=0 pair_diff=1")
        with self.assertRaisesRegex(ValueError, "absent C backend"):
            measure.parse(raw, no_c)
        coverage = manifest()
        zero_c = capture(coverage).replace(b"c_cycles=11", b"c_cycles=0", 1)
        with self.assertRaisesRegex(ValueError, "C timing is zero"):
            measure.parse(zero_c, coverage)

    def test_rejects_fail_expected_vector_mismatch_and_missing_prompt(self):
        coverage = manifest()
        with self.assertRaisesRegex(ValueError, "expected value"):
            measure.parse(capture(coverage, errors=1, bad_expected=True), coverage)
        with self.assertRaisesRegex(ValueError, "prompt"):
            measure.parse(capture(coverage).replace(b"nsh> ", b""), coverage)
        before_done = capture(coverage).replace(b"nsh> \n", b"")
        before_done = before_done.replace(b"AQ_DONE", b"nsh> \nAQ_DONE")
        with self.assertRaisesRegex(ValueError, "must follow AQ_DONE"):
            measure.parse(before_done, coverage)

    def test_serial_reader_ignores_stale_prompt_until_done_and_prompt(self):
        read_fd, write_fd = os.pipe()
        try:
            raw = b"nsh> \r\nAQ_DONE cases=1 samples=64 c_errors=0 rust_errors=0 repeats=5\r\nnsh> "
            os.write(write_fd, raw)
            result = measure._read_serial_until(read_fd, timeout=1)
            self.assertEqual(result, raw)
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_serial_reader_waits_for_fragmented_ansi_prompt(self):
        coverage = manifest()
        complete = capture(coverage).rstrip(b"\n") + b"\x1b[K"
        first, last = complete[:-1], complete[-1:]
        self.assertFalse(measure.DONE_PROMPT_RE.search(first))
        with patch.object(measure.select, 'select', return_value=([123], [], [])), \
                patch.object(measure.os, 'read', side_effect=[first, last]) as reader:
            raw = measure._read_serial_until(123, timeout=1)
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(raw, complete)
        self.assertTrue(measure.parse(raw, coverage)['passed'])

    def test_runner_restores_backup_and_writes_private_proof(self):
        coverage = manifest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "firmware.bin"
            image.write_bytes(b"firmware")
            backup = root / "backup.bin"
            backup.write_bytes(b"private backup")
            out = root / "fresh-output"
            restored = []
            claimed = []

            class FakeDevice:
                @staticmethod
                def flash_command(flasher, port, image):
                    return [flasher, port, str(image)]

                @staticmethod
                def open_serial(port):
                    return 987654

                @staticmethod
                def read_prompt(fd, timeout):
                    return b"nsh> "

                @staticmethod
                def command(fd, text, timeout):
                    self.assertEqual(text, "")
                    self.assertEqual(timeout, 35)
                    return b"\r\nnsh> "

            def fake_run(*args, **kwargs):
                return SimpleNamespace(returncode=0, stdout=b"flash output")

            report = measure.run_device(
                image, backup, out, "fake-port", "fake-flasher", coverage, runs=1,
                device=FakeDevice, restore_fn=lambda *args: restored.append(args),
                runner=fake_run, backup_check=lambda path: "a" * 64,
                serial_reader=lambda fd, command, timeout: capture(coverage),
                exclusive_fn=lambda fd: claimed.append(fd))
            self.assertTrue(report["passed"])
            self.assertEqual(len(restored), 1)
            self.assertEqual(claimed, [987654])
            self.assertEqual((out / "serial.raw").stat().st_mode & 0o077, 0)
            run_raw = (out / "run-001.raw").read_bytes()
            aggregate_raw = (out / "serial.raw").read_bytes()
            start = report["runs"][0]["aggregate_offset"]
            size = report["runs"][0]["raw_bytes"]
            self.assertEqual(aggregate_raw[start:start + size], run_raw)
            self.assertEqual(report["aggregate_raw_sha256"],
                             hashlib.sha256(aggregate_raw).hexdigest())
            self.assertEqual(report["runs"][0]["raw_sha256"],
                             hashlib.sha256(run_raw).hexdigest())
            proof = json.loads((out / "proof.json").read_text())
            self.assertTrue(proof["restoration"]["verified"])
            self.assertEqual(proof["backup_sha256"], "a" * 64)
            public = (out / "summary.json").read_text()
            self.assertNotIn("fake-port", public)
            self.assertNotIn(str(image), public)

    def test_private_proof_retains_exception_message_and_restore_runs(self):
        coverage = manifest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "fw.bin"
            image.write_bytes(b"image")
            backup = root / "backup.bin"
            backup.write_bytes(b"backup")
            out = root / "result"
            restored = []

            class FakeDevice:
                @staticmethod
                def flash_command(*args):
                    return ["flasher"]

            def failed_flash(*args, **kwargs):
                raise OSError("diagnostic failure at /private/device.log")

            with self.assertRaisesRegex(RuntimeError, "backup restored"):
                measure.run_device(
                    image, backup, out, "port", "flasher", coverage, runs=1,
                    device=FakeDevice, restore_fn=lambda *args: restored.append(args),
                    runner=failed_flash, backup_check=lambda path: "b" * 64)
            self.assertEqual(len(restored), 1)
            proof = json.loads((out / "proof.json").read_text())
            self.assertIn("diagnostic failure at /private/device.log",
                          proof["failure_detail"])
            public = (out / "summary.json").read_text()
            self.assertNotIn("/private/device.log", public)

    def test_real_restore_module_exposes_backup_validation_and_restore(self):
        self.assertTrue(callable(measure.RESTORE.backup_identity))
        self.assertTrue(callable(measure.RESTORE.restore))
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "backup.bin"
            with backup.open("wb") as stream:
                stream.truncate(16_777_216)
            backup.chmod(0o600)
            self.assertEqual(len(measure.RESTORE.backup_identity(backup)), 64)


if __name__ == "__main__":
    unittest.main()
