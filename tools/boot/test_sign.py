"""Actual crypto, policy, filesystem and interrupted-issuance controls."""
import copy
import importlib.util
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from cryptography.exceptions import InvalidSignature

SPEC = importlib.util.spec_from_file_location("firmware_sign", Path(__file__).with_name("sign.py"))
SIGN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SIGN)
PASSWORD = b"public-synthetic-test-passphrase-0000000000000000"


class SigningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.authority = self.root / "authority"
        self.authority.mkdir(mode=0o700)
        self.exports = self.root / "exports"
        self.exports.mkdir(mode=0o700)
        self.config = {"schema": 1, "target": "host-public-fixture", "format": SIGN.FORMAT,
                       "configuration_sha256": "a" * 64,
                       "layout": {"flash_bytes": 4 * 1024 * 1024, "primary_offset": 0x20000,
                                  "secondary_offset": 0x60000, "slot_bytes": 0x40000,
                                  "scratch_offset": 0xa0000, "scratch_bytes": 0x10000,
                                  "erase_bytes": 4096, "header_bytes": 32,
                                  "write_align": 4, "max_align": 8, "max_sectors": 128}}
        self.pin = SIGN.initialize(self.authority, self.config, PASSWORD)
        self.policy_data = (self.authority / "policy.json").read_bytes()
        self.policy, self.public = SIGN.public_policy(self.policy_data, self.pin)
        self.body = bytes(range(256)) * 128
        self.output = self.exports / "application.bin"

    def issue(self, counter=1, version="1.2.3+4", output=None, body=None):
        body = self.body if body is None else body
        return SIGN.sign(self.authority, PASSWORD, self.pin, body, version, counter,
                         self.output if output is None else output, SIGN.sha(body))

    def verify(self, data=None, pin=None, target=None, configuration=None, floor=1):
        return SIGN.verify_image(self.output.read_bytes() if data is None else data,
                                 self.policy_data, self.pin if pin is None else pin,
                                 self.config["target"] if target is None else target,
                                 self.config["configuration_sha256"] if configuration is None else configuration,
                                 floor)

    def state(self):
        with SIGN.directory(self.authority, lock=True) as fd:
            return SIGN.state_open(fd, PASSWORD, self.pin)[3]

    def private_key(self):
        with SIGN.directory(self.authority, lock=True) as fd:
            return SIGN.state_open(fd, PASSWORD, self.pin)[2]

    def test_real_signature_and_exact_idempotent_export(self):
        identity = self.issue()
        first = self.output.read_bytes()
        metadata = self.verify()
        self.assertEqual(SIGN.sha(first), identity)
        self.assertEqual(metadata["payload_sha256"], SIGN.sha(self.body))
        self.assertEqual(self.issue(), identity)
        self.assertEqual(self.output.read_bytes(), first)
        self.assertEqual(self.state()["generation"], 1)
        copy_path = self.exports / "recovery-copy.bin"
        self.assertEqual(self.issue(output=copy_path), identity)
        self.assertEqual(copy_path.read_bytes(), first)
        self.assertEqual(self.state()["generation"], 1)

    def test_encrypted_private_key_is_separate_from_public_policy_and_images(self):
        state = json.loads((self.authority / "state.json").read_bytes())
        self.assertIn("BEGIN ENCRYPTED PRIVATE KEY", state["encrypted_key"])
        self.issue()
        self.assertNotIn(b"PRIVATE KEY", self.policy_data)
        self.assertNotIn(b"PRIVATE KEY", self.output.read_bytes())
        self.assertNotIn(PASSWORD, (self.authority / "state.json").read_bytes())
        with self.assertRaises((ValueError, TypeError)):
            with SIGN.directory(self.authority) as fd:
                SIGN.state_open(fd, b"wrong-public-test-passphrase-0000000000000", self.pin)

    def test_security_counter_and_app_version_are_independent(self):
        self.issue(counter=3)
        with self.assertRaises(ValueError):
            self.issue(counter=2, output=self.exports / "old.bin")
        self.assertFalse((self.exports / "old.bin").exists())
        recovery = self.exports / "compatible-recovery.bin"
        self.issue(counter=3, version="0.1.0+1", output=recovery)
        self.verify(recovery.read_bytes(), floor=3)
        with self.assertRaises(ValueError):
            self.verify(recovery.read_bytes(), floor=4)

    def test_policy_pin_target_configuration_and_device_floor_are_required(self):
        self.issue(counter=5)
        for kwargs in [{"pin": "0" * 64}, {"target": "other-board"},
                       {"configuration": "b" * 64}, {"floor": 6}, {"floor": True}]:
            with self.assertRaises(ValueError):
                self.verify(**kwargs)

    def test_reviewer_payload_identity_is_enforced_before_reservation(self):
        with self.assertRaises(ValueError):
            SIGN.sign(self.authority, PASSWORD, self.pin, self.body, "1.0.0+1", 1,
                      self.output, "0" * 64)
        self.assertEqual(self.state()["generation"], 0)
        self.assertFalse(self.output.exists())

    def test_signed_wrong_target_layout_config_counter_and_version_are_refused(self):
        metadata = SIGN.manifest(self.policy, self.pin, self.body, "1.2.3+4", 1)
        for field, value in [("target", "other-board"), ("layout_sha256", "b" * 64),
                             ("configuration_sha256", "b" * 64), ("policy_sha256", "c" * 64),
                             ("payload_sha256", "d" * 64), ("role", "rom-signing")]:
            changed = dict(metadata, **{field: value})
            image, _ = SIGN.produce(self.body, changed, self.policy, self.private_key())
            with self.assertRaises(ValueError, msg=field):
                self.verify(image)

    def test_real_other_key_valid_signature_is_refused(self):
        other = self.root / "other-authority"
        other.mkdir(mode=0o700)
        pin = SIGN.initialize(other, self.config, PASSWORD)
        output = self.exports / "other.bin"
        SIGN.sign(other, PASSWORD, pin, self.body, "1.2.3+4", 1, output, SIGN.sha(self.body))
        with self.assertRaises(ValueError):
            self.verify(output.read_bytes())

    def test_post_signature_header_body_counter_and_signature_corruption(self):
        self.issue()
        data = self.output.read_bytes()
        for offset in [20, 64, 32 + len(self.body) + 8, len(data) - 1]:
            changed = bytearray(data)
            changed[offset] ^= 1
            with self.assertRaises((ValueError, InvalidSignature), msg=str(offset)):
                self.verify(changed)
        for changed in [data[:-1], data + b"\xff", data[:16], b"\x00" * len(data)]:
            with self.assertRaises((ValueError, struct.error)):
                self.verify(changed)

    def test_duplicate_tlvs_and_unknown_unprotected_elements_are_refused(self):
        self.issue()
        data = self.output.read_bytes()
        protected_size = struct.unpack_from("<H", data, 10)[0]
        start = 32 + len(self.body) + protected_size
        extra = struct.pack("<HH", 0x10, 32) + bytes(32)
        changed = bytearray(data + extra)
        struct.pack_into("<H", changed, start + 2, len(changed) - start)
        with self.assertRaises(ValueError):
            self.verify(changed)
        changed = bytearray(data)
        struct.pack_into("<H", changed, start + 4, 0x7777)
        with self.assertRaises(ValueError):
            self.verify(changed)

    def test_duplicate_json_and_invalid_layout_are_refused(self):
        with self.assertRaises(ValueError):
            SIGN.parse(b'{"target":"a","target":"b"}')
        for field, value in [("secondary_offset", 0x20000), ("scratch_offset", 1),
                             ("slot_bytes", True), ("header_bytes", 64),
                             ("erase_bytes", 5000), ("write_align", True)]:
            layout = dict(self.config["layout"], **{field: value})
            with self.assertRaises(ValueError, msg=field):
                SIGN.validate_layout(layout)

    def test_preconfirmed_padded_oversized_and_already_wrapped_inputs_refused(self):
        for body in [b"", b"\x7fELF" + self.body, struct.pack("<I", SIGN.MAGIC) + self.body,
                     bytes(self.config["layout"]["slot_bytes"])]:
            with self.assertRaises(Exception):
                self.issue(body=body)
        self.assertEqual(self.state()["generation"], 0)

    def test_filesystem_aliases_permissions_and_existing_outputs_fail_closed(self):
        state = self.authority / "state.json"
        os.chmod(state, 0o644)
        with self.assertRaises(ValueError):
            self.issue()
        os.chmod(state, 0o600)
        alias = self.root / "state-alias"
        os.link(state, alias)
        with self.assertRaises(ValueError):
            self.issue()
        alias.unlink()
        self.output.write_bytes(b"existing-important-file")
        os.chmod(self.output, 0o600)
        with self.assertRaises(ValueError):
            self.issue()
        self.assertEqual(self.output.read_bytes(), b"existing-important-file")

    def test_symlinked_output_and_parent_are_refused_without_truncation(self):
        existing = self.exports / "important"
        existing.write_bytes(b"keep")
        os.chmod(existing, 0o600)
        self.output.symlink_to(existing)
        with self.assertRaises((ValueError, OSError)):
            self.issue()
        self.assertEqual(existing.read_bytes(), b"keep")
        self.output.unlink()
        alias = self.root / "alias-exports"
        alias.symlink_to(self.exports, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.issue(output=alias / "other.bin")

    def test_authority_lock_refuses_concurrent_issuance(self):
        with SIGN.directory(self.authority, lock=True):
            with self.assertRaises(BlockingIOError):
                self.issue()
        self.assertEqual(self.state()["generation"], 0)

    def test_export_failure_keeps_reserved_signature_and_exact_retry(self):
        with patch.object(SIGN.os, "link", side_effect=OSError("synthetic-public-failure")):
            with self.assertRaises(OSError):
                self.issue(counter=2)
        self.assertFalse(self.output.exists())
        state = self.state()
        self.assertEqual(state["maximum_issued_counter"], 2)
        identity = state["releases"][0]["image_sha256"]
        self.assertEqual(self.issue(counter=2), identity)
        self.assertEqual(self.state()["generation"], 1)

    def test_interrupted_state_commit_blocks_export_until_authenticated_recovery(self):
        with patch.object(SIGN.os, "replace", side_effect=OSError("synthetic-public-failure")):
            with self.assertRaises(OSError):
                self.issue(counter=4)
        self.assertFalse(self.output.exists())
        self.assertTrue((self.authority / ".state-next").exists())
        self.assertEqual(self.state()["generation"], 0)
        with self.assertRaises(ValueError):
            self.issue(counter=4)
        with self.assertRaises(ValueError):
            SIGN.recover(self.authority, PASSWORD, self.pin, discard_incomplete=True)
        SIGN.recover(self.authority, PASSWORD, self.pin)
        self.assertEqual(self.state()["maximum_issued_counter"], 4)
        self.issue(counter=4)
        self.verify(floor=4)
        self.assertEqual(self.state()["generation"], 1)

    def test_partial_journal_needs_explicit_discard_and_cannot_publish(self):
        pending = self.authority / ".state-next"
        pending.write_bytes(b'{"incomplete":')
        os.chmod(pending, 0o600)
        with self.assertRaises(ValueError):
            self.issue()
        with self.assertRaises(ValueError):
            SIGN.recover(self.authority, PASSWORD, self.pin)
        self.assertFalse(self.output.exists())
        SIGN.recover(self.authority, PASSWORD, self.pin, discard_incomplete=True)
        self.assertFalse(pending.exists())
        self.issue()

    def test_post_link_failure_recovers_only_its_exact_owned_alias(self):
        real_link = SIGN.os.link
        def linked_then_failed(*args, **kwargs):
            real_link(*args, **kwargs)
            raise OSError("synthetic-public-after-link")
        with patch.object(SIGN.os, "link", side_effect=linked_then_failed):
            with self.assertRaises(OSError):
                self.issue()
        self.assertEqual(self.output.stat().st_nlink, 2)
        identity = self.state()["releases"][0]["image_sha256"]
        self.assertEqual(self.issue(), identity)
        self.assertEqual(self.output.stat().st_nlink, 1)
        self.assertFalse(list(self.exports.glob(".nxrs-release-*")))

    def test_arbitrary_output_hardlink_is_not_recovery_authority(self):
        self.issue()
        alias = self.exports / "unrecognized-alias"
        os.link(self.output, alias)
        with self.assertRaises((ValueError, OSError)):
            self.issue()
        self.assertTrue(alias.exists())
        self.assertEqual(self.output.stat().st_nlink, 2)

    def test_interrupted_staging_write_reconstructs_exact_committed_image(self):
        real_write = SIGN.write_new
        def partial_export(parent, name, data):
            if name.startswith(".nxrs-release-"):
                real_write(parent, name, data[:73])
                raise OSError("synthetic-public-partial-export")
            return real_write(parent, name, data)
        with patch.object(SIGN, "write_new", side_effect=partial_export):
            with self.assertRaises(OSError):
                self.issue()
        self.assertFalse(self.output.exists())
        self.assertEqual(len(list(self.exports.glob(".nxrs-release-*"))), 1)
        identity = self.state()["releases"][0]["image_sha256"]
        self.assertEqual(self.issue(), identity)
        self.assertEqual(self.state()["generation"], 1)
        self.verify()

    def test_corrupt_or_foreign_staging_is_preserved_and_refused(self):
        with patch.object(SIGN.os, "link", side_effect=OSError("synthetic-public-pre-link")):
            with self.assertRaises(OSError):
                self.issue()
        staging = next(self.exports.glob(".nxrs-release-*"))
        staging.write_bytes(b"unrecognized-important-data")
        with self.assertRaises(ValueError):
            self.issue()
        self.assertEqual(staging.read_bytes(), b"unrecognized-important-data")
        self.assertFalse(self.output.exists())

    def test_authenticated_inconsistent_journal_cannot_be_discarded(self):
        self.issue()
        current = self.state()
        inconsistent = dict(current, maximum_issued_counter=8)
        pending = self.authority / ".state-next"
        pending.write_bytes(SIGN.canonical(SIGN.envelope(inconsistent, self.private_key(), b"state")))
        os.chmod(pending, 0o600)
        for discard in [False, True]:
            with self.assertRaises(ValueError):
                SIGN.recover(self.authority, PASSWORD, self.pin, discard)
            self.assertTrue(pending.exists())

    def test_retry_repeats_the_authority_durability_barrier_before_export(self):
        with patch.object(SIGN.os, "link", side_effect=OSError("synthetic-public-pre-link")):
            with self.assertRaises(OSError):
                self.issue()
        self.assertFalse(self.output.exists())
        with patch.object(SIGN.os, "fsync", side_effect=OSError("synthetic-public-fsync-failure")):
            with self.assertRaises(OSError):
                self.issue()
        self.assertFalse(self.output.exists())
        self.issue()
        self.verify()

    def kill_at_barrier(self, phase):
        source = self.root / "input.bin"
        source.write_bytes(self.body)
        password_read, password_write = os.pipe()
        checkpoint_read, checkpoint_write = os.pipe()
        helper = Path(__file__).resolve().parents[2] / "tests/boot/signing_process.py"
        command = [sys.executable, str(helper), "--authority", str(self.authority),
                   "--input", str(source), "--output", str(self.output),
                   "--policy-sha256", self.pin, "--password-fd", str(password_read),
                   "--checkpoint-fd", str(checkpoint_write), "--phase", phase]
        child = subprocess.Popen(command, pass_fds=(password_read, checkpoint_write),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True,
                                 env={"PATH": os.environ["PATH"], "HOME": str(self.root),
                                      "PYTHONNOUSERSITE": "1"})
        os.close(password_read)
        os.close(checkpoint_write)
        try:
            os.write(password_write, PASSWORD + b"\n")
            os.close(password_write)
            password_write = None
            self.assertTrue(select.select([checkpoint_read], [], [], 10)[0])
            self.assertEqual(os.read(checkpoint_read, 16), b"ready")
            metadata = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(child.pid)],
                                      capture_output=True, check=True, timeout=3).stdout
            self.assertNotIn(PASSWORD, metadata)
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
            self.assertEqual(child.returncode, -signal.SIGKILL)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
            if password_write is not None:
                os.close(password_write)
            os.close(checkpoint_read)

    def test_actual_process_kill_before_and_after_ledger_replace(self):
        self.kill_at_barrier("pre-replace")
        self.assertEqual(self.state()["generation"], 0)
        self.assertFalse(self.output.exists())
        SIGN.recover(self.authority, PASSWORD, self.pin)
        expected = self.state()["releases"][0]["image_sha256"]
        self.assertEqual(self.issue(counter=2), expected)
        self.verify(floor=2)
        # A distinct request reaches replacement again after a durable first release.
        self.output.unlink()
        for entry in list(self.exports.glob(".nxrs-release-*")):
            entry.unlink()
        # Use a fresh authority to ensure the child must execute the state transaction.
        other = self.root / "post-replace-authority"
        other.mkdir(mode=0o700)
        self.authority = other
        self.pin = SIGN.initialize(other, self.config, PASSWORD)
        self.policy_data = (other / "policy.json").read_bytes()
        self.kill_at_barrier("post-replace")
        self.assertEqual(self.state()["generation"], 1)
        self.assertFalse(self.output.exists())
        expected = self.state()["releases"][0]["image_sha256"]
        self.assertEqual(self.issue(counter=2), expected)
        self.verify(floor=2)

    def test_actual_process_kill_after_staging_and_after_atomic_link(self):
        self.kill_at_barrier("pre-link")
        self.assertFalse(self.output.exists())
        expected = self.state()["releases"][0]["image_sha256"]
        self.assertEqual(self.issue(counter=2), expected)
        self.output.unlink()
        # Retry of the same durable request still exercises an actual new publication.
        self.kill_at_barrier("post-link")
        self.assertEqual(self.output.stat().st_nlink, 2)
        self.assertEqual(self.issue(counter=2), expected)
        self.assertEqual(self.output.stat().st_nlink, 1)
        self.assertEqual(self.state()["generation"], 1)
        self.verify(floor=2)

    def test_password_without_eof_is_rejected_within_deadline(self):
        read, write = os.pipe()
        self.addCleanup(os.close, write)
        os.write(write, PASSWORD)
        started = time.monotonic()
        with self.assertRaises(ValueError):
            SIGN.read_password(read)
        self.assertLess(time.monotonic() - started, 5)

    def test_public_key_export_requires_exact_target_and_configuration(self):
        output = self.exports / "application-public.pem"
        SIGN.export_public(self.policy_data, self.pin, self.config["target"],
                           self.config["configuration_sha256"], output)
        self.assertEqual(output.read_bytes(), self.policy["public_key"].encode())
        self.assertNotIn(b"PRIVATE KEY", output.read_bytes())
        with self.assertRaises(ValueError):
            SIGN.export_public(self.policy_data, self.pin, "other-board",
                               self.config["configuration_sha256"], self.exports / "wrong.pem")
        self.assertFalse((self.exports / "wrong.pem").exists())

    def test_state_authentication_precedes_encrypted_key_parsing(self):
        path = self.authority / "state.json"
        changed = json.loads(path.read_bytes())
        changed["encrypted_key"] = "malformed encrypted material"
        path.write_bytes(SIGN.canonical(changed))
        with patch.object(SIGN.serialization, "load_pem_private_key") as decrypt:
            with self.assertRaises(InvalidSignature):
                self.issue()
            decrypt.assert_not_called()

    def test_compiled_trailer_sector_budget_controls_the_size_boundary(self):
        key = self.private_key()
        metadata = SIGN.manifest(self.policy, self.pin, self.body, "1.2.3+4", 1)
        small, _ = SIGN.produce(self.body, metadata, self.policy, key)
        overhead = len(small) - len(self.body)
        layout = self.config["layout"]
        budget = layout["slot_bytes"] - (3 * 128 * 4 + 48) - overhead
        fitting = bytes(budget)
        self.issue(body=fitting)
        self.verify()
        with self.assertRaises(Exception):
            self.issue(body=bytes(budget + 1), output=self.exports / "oversized.bin")
        self.assertEqual(self.state()["generation"], 1)
        self.assertFalse((self.exports / "oversized.bin").exists())

    def test_actual_cli_uses_private_pipe_and_verifies_public_inputs(self):
        script = Path(__file__).with_name("sign.py").resolve()
        source = self.root / "cli-input.bin"
        source.write_bytes(self.body)
        read, write = os.pipe()
        os.write(write, PASSWORD + b"\n")
        os.close(write)
        try:
            result = subprocess.run([sys.executable, str(script), "sign", "--authority", str(self.authority),
                "--policy-sha256", self.pin, "--input", str(source), "--output", str(self.output),
                "--payload-sha256", SIGN.sha(self.body), "--version", "1.2.3+4", "--security-counter", "1",
                "--password-fd", str(read)], pass_fds=(read,), capture_output=True, timeout=10)
        finally:
            os.close(read)
        self.assertEqual(result.returncode, 0)
        self.assertIn(SIGN.sha(self.output.read_bytes()).encode(), result.stdout)
        self.assertNotIn(PASSWORD, result.stdout + result.stderr)
        arguments = [sys.executable, str(script), "verify", "--policy", str(self.authority / "policy.json"),
            "--policy-sha256", self.pin, "--image", str(self.output), "--configuration-sha256",
            self.config["configuration_sha256"], "--minimum-security-counter", "1", "--target"]
        result = subprocess.run(arguments + [self.config["target"]], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        denied = subprocess.run(arguments + ["other-board"], capture_output=True, timeout=10)
        self.assertEqual(denied.returncode, 1)
        self.assertEqual(denied.stderr, b"Firmware authority operation failed; no activation is performed\n")

    def test_named_and_unlinked_named_fifo_password_channels_are_refused(self):
        path = self.root / "named-password-fifo"
        os.mkfifo(path, 0o600)
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with self.assertRaises(ValueError):
            SIGN.read_password(fd)
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        path.unlink()
        with self.assertRaises(ValueError):
            SIGN.read_password(fd)

    def test_oversized_image_is_refused_before_private_key_signing(self):
        key = self.private_key()
        oversized = bytes(self.config["layout"]["slot_bytes"] - 100)
        metadata = SIGN.manifest(self.policy, self.pin, oversized, "1.2.3+4", 1)
        with patch.object(SIGN.RSA, "sign", side_effect=AssertionError("sign must not run")) as signer:
            with self.assertRaises(ValueError):
                SIGN.produce(oversized, metadata, self.policy, key)
            signer.assert_not_called()

    def test_password_input_requires_private_pipe_and_eof(self):
        read, write = os.pipe()
        os.write(write, PASSWORD + b"\n")
        os.close(write)
        self.assertEqual(SIGN.read_password(read), PASSWORD)
        with tempfile.TemporaryFile() as file:
            duplicate = os.dup(file.fileno())
            with self.assertRaises(ValueError):
                SIGN.read_password(duplicate)
        read, write = os.pipe()
        self.addCleanup(os.close, write)
        os.write(write, b"a" * 1026)
        with self.assertRaises(ValueError):
            SIGN.read_password(read)


if __name__ == "__main__":
    unittest.main()
