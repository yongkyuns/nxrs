"""Fail-closed NuttX admission execution oracle controls; not hardware evidence."""
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import qualify_admission_target as target


class ExecutionOracleTests(unittest.TestCase):
    def setUp(self):
        self.labels = ["positive", "signed-wrong-target"]
        self.prefixes = 17
        self.lines = ["IMAGE_ADMISSION_CASE " + label + " passed" for label in self.labels]
        self.summary = "IMAGE_ADMISSION_QUALIFIED cases=2 proper_prefixes=17 corruptions=10000"
        self.text = "\n".join(["IMAGE_ADMISSION_ROOT_BINDING passed"] + self.lines + [self.summary]) + "\n"
        self.refusal = "IMAGE_ADMISSION_EXPECTED_REFUSAL floor\n"

    def verify(self, text, status="IMAGE_ADMISSION_STATUS_0\n", mode="pass"):
        target.verify_execution(text, status, mode, self.labels, self.prefixes)

    def test_actual_backend_preflight_failure_emits_a_bounded_marker(self):
        child = subprocess.run(["bash", str(target.ROOT / "tools/build-nuttx-std-app.sh")],
            capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(child.returncode, 2)
        diagnostic = target.build_diagnostic(child.stdout, child.stderr)
        self.assertEqual(diagnostic, "NuttX backend refused before a tracked command status=2")

    def test_actual_failed_command_reports_its_line_without_command_text(self):
        prefix = (target.ROOT / "tools/build-nuttx-std-app.sh").read_text().split("\nROOT=", 1)[0]
        command = "test 'private-password-sensitive' = refusal"
        source = prefix + "\n" + command + "\n"
        expected = source.splitlines().index(command) + 1
        with tempfile.TemporaryDirectory() as directory:
            script = target.Path(directory) / "probe.sh"
            script.write_text(source)
            child = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=5)
        self.assertEqual(child.returncode, 1)
        self.assertEqual(target.build_diagnostic(child.stdout, child.stderr),
                         "NuttX backend refused at build-nuttx-std-app.sh:" + str(expected) + " status=1")
        self.assertNotIn("private-password-sensitive", child.stderr)

    def test_diagnostics_publish_known_stage_and_declared_profile_requirements_only(self):
        text = "\n".join(("NXRS_NUTTX_BUILD_STAGE=rust", "NXRS_NUTTX_BUILD_STAGE=native",
            "NXRS_NUTTX_BUILD_STAGE=private-password-sensitive",
            "Deployment nxrs-image-admission-qualification-esp32s3-update-qemu unresolved requirement: CONFIG_MTD_BYTE_WRITE=y",
            "Deployment nxrs-image-admission-qualification-esp32s3-update-qemu unresolved requirement: private-password-sensitive"))
        self.assertEqual(target.build_diagnostic("", text, "esp32s3-update-qemu"),
                         "NuttX last build stage: native\nNuttX unresolved profile requirement: CONFIG_MTD_BYTE_WRITE=y")

    def test_child_failure_diagnostics_retain_only_known_source_lines_and_codes(self):
        private = "private-password-" + "sensitive"
        stdout = '{"reason":"compiler-message","message":{"code":{"code":"E0308"},"message":"' + private + '"}}'
        stderr = "\n".join(("NXRS_NUTTX_BUILD_FAILURE line=123 status=1",
            "Active std app missing expected import pthread_create",
            "Missing prerequisite: kconfig-tweak", "error[E0425]: " + private,
            "NXRS_NUTTX_BUILD_FAILURE line=999999 status=1",
            "NXRS_NUTTX_BUILD_FAILURE line=2 status=999", private))
        result = target.build_diagnostic(stdout, stderr)
        self.assertEqual(result.splitlines(), [
            "NuttX Rust diagnostic codes: E0308,E0425",
            "NuttX active ABI import absent: pthread_create",
            "NuttX backend refused at build-nuttx-std-app.sh:123 status=1",
            "NuttX missing prerequisite: kconfig-tweak"])
        self.assertNotIn(private, result)
        self.assertEqual(target.build_diagnostic(private, private), "")

    def test_staging_requires_every_ordered_case_status_and_explicit_no_activation(self):
        labels = ["offline-signer-positive", "signed-wrong-target", "older-version-permitted-by-security-floor"]
        lines = ["IMAGE_STAGING_CASE " + name + " passed" for name in target.admission.staging_labels(labels)]
        summary = "IMAGE_STAGING_QUALIFIED cases=3 activation=false"
        text = "\n".join(lines + [summary]) + "\n"
        target.verify_execution(text, "IMAGE_ADMISSION_STATUS_0\n", "staging", labels, 0)
        for changed in (text.replace(lines[0] + "\n", ""), text + lines[0] + "\n",
                        "\n".join(list(reversed(lines)) + [summary]), text.replace("activation=false", "activation=true"),
                        text + summary + "\n", text + "PANIC\n"):
            with self.assertRaises(ValueError):
                target.verify_execution(changed, "IMAGE_ADMISSION_STATUS_0\n", "staging", labels, 0)
        with self.assertRaises(ValueError):
            target.verify_execution(text, "IMAGE_ADMISSION_STATUS_1\n", "staging", labels, 0)

    def test_emulated_flash_requires_exact_secondary_bytes_erased_tail_and_unchanged_other_regions(self):
        layout = dict(flash_bytes=65536, secondary_offset=20480, slot_bytes=16384, max_sectors=4, write_align=4)
        before = bytearray([0x5a] * 65536)
        before[20480:36864] = bytes([255]) * 16384
        staged = b"actual-qualified-image"
        after = bytearray(before)
        after[20480:20480 + len(staged)] = staged
        target.verify_flash(before, after, layout, staged)
        for offset in (0, 4096, 20479, 20480, 20480 + len(staged), 36863, 36864, 65535):
            corrupt = bytearray(after)
            corrupt[offset] ^= 1
            with self.assertRaises(ValueError):
                target.verify_flash(before, corrupt, layout, staged)
        dirty = bytearray(before)
        dirty[20480] = 0
        with self.assertRaises(ValueError):
            target.verify_flash(dirty, after, layout, staged)
        with self.assertRaises(ValueError):
            target.verify_flash(before, after[:-1], layout, staged)

    def test_live_tftp_and_unsupported_hosts_are_refused_before_builds(self):
        with patch.object(target.platform, "system", return_value="Linux"), \
                patch.object(target.platform, "machine", return_value="x86_64"), \
                patch.object(target.Path, "is_dir", return_value=True), \
                patch.object(target.os, "access", return_value=True):
            with self.assertRaises(ValueError):
                target.nuttx_host_preflight()
        with patch.object(target.platform, "system", return_value="Darwin"):
            with self.assertRaises(ValueError):
                target.nuttx_host_preflight()

    def test_exact_success_and_actual_nonzero_refusal_are_distinct(self):
        self.verify(self.text)
        self.verify(self.refusal, "IMAGE_ADMISSION_STATUS_1\n", "floor-refusal")
        self.verify(self.text.replace("\n", "\r\n"))

    def test_missing_duplicate_reordered_extra_and_wrong_results_are_refused(self):
        for text in (self.text.replace(self.lines[0] + "\n", ""), self.text + self.lines[0] + "\n",
                     "\n".join(list(reversed(self.lines)) + [self.summary]),
                     self.text + "IMAGE_ADMISSION_CASE unexpected passed\n",
                     self.text.replace("corruptions=10000", "corruptions=1"),
                     self.text.replace("proper_prefixes=17", "proper_prefixes=16"),
                     self.text + self.summary + "\n", self.text + "PANIC\n",
                     self.text + "thread panicked at\n", self.text + self.refusal):
            with self.assertRaises(ValueError):
                self.verify(text)

    def test_root_binding_marker_is_mandatory_and_unique(self):
        for text in (self.text.replace("IMAGE_ADMISSION_ROOT_BINDING passed\n", ""),
                     self.text + "IMAGE_ADMISSION_ROOT_BINDING passed\n"):
            with self.assertRaises(ValueError):
                self.verify(text)

    def test_root_refusal_requires_its_own_marker_and_zero_status(self):
        text = "IMAGE_STAGING_ROOT_REFUSAL passed\n"
        self.verify(text, mode="root-refusal")
        for bad_text, status in (("", "0"), (text * 2, "0"), (text, "1"),
                                 (text + self.text, "0"), (text + "PANIC\n", "0")):
            with self.assertRaises(ValueError):
                self.verify(bad_text, "IMAGE_ADMISSION_STATUS_" + status + "\n", "root-refusal")

    def test_root_refusal_requires_every_flash_byte_to_remain_unchanged(self):
        layout = dict(flash_bytes=65536)
        before = bytes(range(256)) * 256
        target.verify_refusal_flash(before, before, layout)
        for offset in (0, 4096, 20480, 36864, 65535):
            after = bytearray(before)
            after[offset] ^= 1
            with self.assertRaises(ValueError):
                target.verify_refusal_flash(before, after, layout)
        for before_value, after_value in ((before[:-1], before[:-1]), (before, before[:-1])):
            with self.assertRaises(ValueError):
                target.verify_refusal_flash(before_value, after_value, layout)

    def test_status_and_negative_oracle_cannot_turn_a_failure_into_success(self):
        for text, status, mode in ((self.text, "IMAGE_ADMISSION_STATUS_1\n", "pass"),
                                  (self.text, "IMAGE_ADMISSION_STATUS_0\n" * 2, "pass"),
                                  (self.refusal, "IMAGE_ADMISSION_STATUS_0\n", "floor-refusal"),
                                  (self.refusal * 2, "IMAGE_ADMISSION_STATUS_1\n", "floor-refusal"),
                                  ("", "IMAGE_ADMISSION_STATUS_1\n", "floor-refusal"),
                                  (self.refusal + self.summary, "IMAGE_ADMISSION_STATUS_1\n", "floor-refusal"),
                                  (self.refusal, "IMAGE_ADMISSION_STATUS_1\n", "unknown")):
            with self.assertRaises(ValueError):
                self.verify(text, status, mode)


if __name__ == "__main__":
    unittest.main()
