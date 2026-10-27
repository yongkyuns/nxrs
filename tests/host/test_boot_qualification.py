"""Failure controls for the external native boot qualification boundary."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("boot_qualification", ROOT / "tools/boot/qualify.py")
BOOT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BOOT)


class BootQualificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def archive(self, name, kind=tarfile.REGTYPE, size=4):
        path = self.root / "source.tar"
        with tarfile.open(path, "w") as output:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = size if kind == tarfile.REGTYPE else 0
            member.linkname = "/outside"
            output.addfile(member, io.BytesIO(b"test") if kind == tarfile.REGTYPE else None)
        return path

    def test_actual_archive_extraction_and_traversal_refusal(self):
        good = self.archive("source/input")
        self.assertEqual((BOOT.extract(good, self.root / "good") / "input").read_bytes(), b"test")
        for index, name in enumerate(["../outside", "/outside", "source/../../outside"]):
            with self.assertRaises(ValueError):
                BOOT.extract(self.archive(name), self.root / f"bad{index}")

    def test_archive_links_and_devices_are_refused(self):
        for index, kind in enumerate([tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE]):
            with self.assertRaises(ValueError):
                BOOT.extract(self.archive("source/input", kind), self.root / str(index))

    def test_artifact_digest_and_symlink_refusal(self):
        path = self.root / "input"
        path.write_bytes(b"reviewed")
        expected = BOOT.digest(path)
        BOOT.verify(path, expected)
        link = self.root / "link"
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            BOOT.verify(link, expected)
        path.write_bytes(b"modified")
        with self.assertRaises(ValueError):
            BOOT.verify(path, expected)

    def clean_audit(self):
        return {"settings": {"ignore": []}, "database": {"advisory-count": 1, "last-commit": "public"},
                "lockfile": {"dependency-count": 89},
                "vulnerabilities": {"found": False, "count": 0, "list": []}, "warnings": {}}

    def test_json_success_cannot_hide_vulnerability_or_warnings(self):
        report = self.clean_audit()
        BOOT.audit_result(0, json.dumps(report))
        cases = []
        vulnerable = copy.deepcopy(report)
        vulnerable["vulnerabilities"] = {"found": True, "count": 1, "list": [{"advisory": {"id": "RUSTSEC-2025-0009"}}]}
        cases.append(vulnerable)
        for category in ["unmaintained", "unsound", "yanked", "notice"]:
            warned = copy.deepcopy(report)
            warned["warnings"] = {category: [{"package": "affected"}]}
            cases.append(warned)
        exempted = copy.deepcopy(report)
        exempted["settings"]["ignore"] = ["RUSTSEC-2025-0009"]
        cases.append(exempted)
        for invalid in cases:
            with self.assertRaises(ValueError):
                BOOT.audit_result(0, json.dumps(invalid))
        with self.assertRaises(ValueError):
            BOOT.audit_result(1, json.dumps(report))

    def test_negative_control_requires_actual_affected_identity(self):
        clean = self.clean_audit()
        with self.assertRaises(ValueError):
            BOOT.audit_result(1, json.dumps(clean), negative=True)
        clean["vulnerabilities"] = {"found": True, "count": 1, "list": [{"advisory": {"id": "RUSTSEC-2025-0009"}}]}
        BOOT.audit_result(1, json.dumps(clean), negative=True)
        with self.assertRaises(ValueError):
            BOOT.audit_result(0, json.dumps(clean), negative=True)

    def test_missing_filtered_failed_and_ignored_policy_tests_refused(self):
        name = BOOT.POLICY_TESTS[0]
        BOOT.require_tests("test image::nxrs_policy::" + name + " ... ok\n", [name], "image::nxrs_policy::")
        for text in ["", "0 tests passed", "test " + name + " ... FAILED",
                     "test image::nxrs_policy::" + name + " ... ignored"]:
            with self.assertRaises(ValueError):
                BOOT.require_tests(text, [name], "image::nxrs_policy::")

    def test_native_state_format_requires_all_trial_and_revert_observations(self):
        # Format controls only; actual trailer/state evidence comes from the SDK.
        names = ["initial", "pending", "trial", "reverted", "confirmed", "permanent"]
        names += [prefix + str(i) for i in range(1, 3) for prefix in ("cut-", "trial-resumed-", "reverted-")]
        names += [prefix + str(i) for i in range(1, 3) for prefix in ("revert-cut-", "revert-resumed-")]
        raw = b"NXRSBT02" + (2).to_bytes(4, "little") * 2
        for name in names:
            raw += bytes([len(name)]) + name.encode() + (131072).to_bytes(4, "little") + bytes([255]) * 144
        operations, reverts, records = BOOT.boot_state_records(raw)
        self.assertEqual((operations, reverts, list(records)), (2, 2, names))
        begin, _, end = records["cut-2"]
        malformed = [raw[:-1], raw + b"extra", raw[:begin] + raw[end:],
                     b"NXRSBT01" + raw[8:], raw[:8] + bytes(4) + raw[12:],
                     raw[:8] + (4097).to_bytes(4, "little") + raw[12:]]
        for value in malformed:
            with self.assertRaises(ValueError):
                BOOT.boot_state_records(value)


if __name__ == "__main__":
    unittest.main()
