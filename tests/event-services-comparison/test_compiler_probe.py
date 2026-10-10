import argparse
from collections import Counter
import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("compiler_probe_tests", HERE / "compiler_probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)

ASM = """\t.literal_position
\t.literal .LCPI0_0, 123
\t.text
\t.global probe_rust
\t.p2align 2
\t.type probe_rust,@function
probe_rust:
\tentry a1, 32
\tloop a4, .LBB0_3
\txor a11, a8, a3
\tssai 27
\tsrc a12, a2, a2
\tmull a12, a12, a9
\tadd.n a2, a11, a12
\tadd.n a8, a8, a10
.LBB0_3:
\tretw.n
.Lfunc_end0:
\t.size probe_rust, .Lfunc_end0-probe_rust
"""


def fixture():
    rows = []
    for repeat in range(9):
        expected = probe.reference(0x12345678 + repeat, 0x87654321 ^ repeat, 512)
        for compiler in ("c_gcc", "c_llvm", "rust_llvm"):
            for placement in ("flash", "iram"):
                for offset in probe.OFFSETS:
                    address = (0x42001000 if placement == "flash" else 0x40371000) + offset
                    rows.append(f"CP_ROW mode=short repeat={repeat} case=cp_{compiler}_{placement}_{offset:02d} address={address} iterations=512 cycles=4000 result={expected} expected={expected}")
    rows.append("CP_DONE mode=short cases=48 repeats=9 errors=0 competitor_jobs=0")
    return ("\n".join(rows) + "\nnsh> ").encode()


class CompilerProbeTests(unittest.TestCase):
    def test_published_cohorts_retain_all_samples_and_compiled_patch_identity(self):
        data = json.loads((HERE / "results/esp32s3-compiler-isolation-2026-10-05.json").read_text())
        root = HERE.parents[1]
        pins = json.loads((root / "upstream/rust-llvm/upstream.json").read_text())
        self.assertEqual(data["llvm_revision"], pins["revision"])
        self.assertEqual(data["llvm_tests"]["passed"], pins["qualification_tests"]["total"])
        self.assertEqual(len(data["before"]["patches"]), 5)
        self.assertEqual(len(data["after"]["patches"]), 6)
        self.assertEqual(data["before"]["sources"], data["after"]["sources"])
        self.assertEqual(data["before"]["configuration_sha256"], data["after"]["configuration_sha256"])
        for name in ("before", "after"):
            cohort = data[name]
            self.assertTrue(cohort["restore_verified"])
            samples = 0
            for mode, result in cohort["modes"].items():
                count, repeats = probe.MODES[mode]
                self.assertEqual(result["iterations"], count)
                self.assertEqual(set(result["cycles"]), set(cohort["addresses"]))
                self.assertEqual(len(result["cycles"]), 48)
                for cycles in result["cycles"].values():
                    self.assertEqual(len(cycles), repeats)
                    self.assertTrue(all(c > 0 for c in cycles))
                    samples += len(cycles)
            self.assertEqual(samples, 864)
            for entry in cohort["patches"]:
                digest = hashlib.sha256((root / "upstream/rust-llvm/patches" / entry["name"]).read_bytes()).hexdigest()
                self.assertEqual(entry["sha256"], digest)
        for cohort, expected in ((data["before"], {3605: 143, 3606: 1}),
                                 (data["after"], {3093: 144})):
            samples = [c for case, values in cohort["modes"]["short"]["cycles"].items()
                       if "rust_llvm" in case for c in values]
            self.assertEqual(Counter(samples), expected)
        self.assertNotIn("/Users/", json.dumps(data))
        self.assertNotIn("usbmodem", json.dumps(data))

    def test_public_cohort_revalidates_serial_and_excludes_private_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            raw = fixture()
            parsed = probe.validate(raw, "short")
            digest = hashlib.sha256(raw).hexdigest()
            (base / "short.serial").write_bytes(raw)
            record = {"failure": None, "restore_error": None, "restore_verified": True,
                      "image_sha256": "image", "runs": [{**parsed, "raw_sha256": digest}],
                      "backup_sha256": "private"}
            (base / "measurement.json").write_text(json.dumps(record))
            build = {"artifacts": {"image.bin": "image"}, "configuration_sha256": "config",
                     "sources": {"core.rs": "source", "compiler_probe.py": "tool"},
                     "layout": {"source_sha256": {"rust_llvm": "asm"}},
                     "compiler_patch_ledger": {"patches": [{"name": "patch", "sha256": "hash",
                                                            "private_path": "/home/private"}]}}
            build_path = base / "build.json"
            build_path.write_text(json.dumps(build))
            with patch.object(probe, "MODES", {"short": (512, 9)}):
                public = probe.cohort(base, build_path)
                self.assertEqual(len(public["modes"]["short"]["cycles"]), 48)
                self.assertNotIn("private", json.dumps(public))
                self.assertEqual(public["sources"], {"core.rs": "source"})
                record["restore_verified"] = False
                (base / "measurement.json").write_text(json.dumps(record))
                with self.assertRaisesRegex(ValueError, "unverified"):
                    probe.cohort(base, build_path)
                record["restore_verified"] = True
                (base / "measurement.json").write_text(json.dumps(record))
                (base / "short.serial").write_bytes(raw + b"changed")
                with self.assertRaisesRegex(ValueError, "hash mismatch"):
                    probe.cohort(base, build_path)

    def test_stale_prompt_or_partial_marker_cannot_complete_read(self):
        self.assertFalse(probe.complete(b"nsh> \r\nCP_DONE mode=short"))
        self.assertFalse(probe.complete(b"CP_DONE mode=short\r\n"))
        self.assertTrue(probe.complete(b"nsh> \r\nCP_DONE mode=short\r\nnsh> "))

    def test_clones_preserve_instruction_order_and_uniquely_scope_labels(self):
        lines = probe.function_assembly(ASM, "probe_rust")
        cloned = probe.clone_function(lines, "probe_rust", "new", ".iram1.new", 18)
        self.assertIn(".balign 32\n\t.space 18,0\nnew:", cloned)
        self.assertIn(".LnewBB0_3:", cloned)
        self.assertNotIn("probe_rust", cloned)
        instructions = lambda text: [line.strip() for line in text.splitlines()
                                     if line.startswith("\t") and not line.strip().startswith(".")]
        before = instructions(ASM)
        after = instructions(cloned)
        self.assertEqual(after, [line.replace(".LBB0_3", ".LnewBB0_3") for line in before])

    def test_missing_or_ambiguous_function_is_rejected(self):
        for text in ("", ASM + ASM, ASM.replace(".size", ".other")):
            with self.assertRaises(ValueError):
                probe.function_assembly(text, "probe_rust")

    def test_reference_boundary_counts(self):
        self.assertEqual(probe.reference(123, 456, 0), 123)
        self.assertEqual(probe.reference(1, 0, 1), (32 * 0x9e3779b9) & 0xffffffff)

    def test_complete_output_checks_all_layouts_repeats_and_results(self):
        parsed = probe.validate(fixture(), "short")
        self.assertEqual(len(parsed["rows"]), 432)

    def test_invalid_or_missing_row_cannot_be_accepted(self):
        valid = fixture()
        changed = [valid.replace(b"cycles=4000", b"cycles=0", 1),
                   valid.replace(b"address=1107300352", b"address=1107300353", 1),
                   valid.split(b"\n", 1)[1],
                   valid + b"\n" + valid.split(b"\n", 1)[0] + b"\n",
                   valid.replace(b"errors=0", b"errors=1"),
                   valid.replace(b"competitor_jobs=0", b"competitor_jobs=1")]
        for index, raw in enumerate(changed):
            with self.subTest(index=index), self.assertRaises(ValueError):
                probe.validate(raw, "short")

    def test_flashing_failure_still_restores_and_records(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            image = base / "image.bin"
            image.write_bytes(b"test")
            args = argparse.Namespace(image=image, backup=base / "backup", out=base / "out",
                                      flasher="esptool", port="test", mode=["short"])
            failure = probe.subprocess.CompletedProcess([], 1, "failed", "")
            with patch.object(probe.matrix, "backup_identity", return_value="hash"), \
                 patch.object(probe.subprocess, "run", return_value=failure), \
                 patch.object(probe.matrix, "restore") as restore:
                with self.assertRaises(probe.subprocess.CalledProcessError):
                    probe.measure(args)
            restore.assert_called_once()
            self.assertIn('"restore_verified":true', (args.out / "measurement.json").read_text())


if __name__ == "__main__":
    unittest.main()
