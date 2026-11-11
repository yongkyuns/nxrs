"""Host/control witnesses for the harness, never NuttX/device success evidence."""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / "tests/nuttx-std/ssh"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


SSH = module("ssh_qualification", DIRECTORY / "run.py")
CONSOLE = module("ssh_console", DIRECTORY / "console.py")
BUILD = module("ssh_build", DIRECTORY / "check-build.py")
NATIVE = module("dropbear_native_fixture", ROOT / "tests/host/test_dropbear_native.py")


class SshQualificationTests(unittest.TestCase):
    def test_descriptor_block_must_fit_the_pinned_fd_set(self):
        config = {"CONFIG_NFILE_DESCRIPTORS_PER_BLOCK": "256", "CONFIG_LIBC_OPEN_MAX": "256"}
        BUILD.check_descriptor_configuration(config)
        config["CONFIG_NFILE_DESCRIPTORS_PER_BLOCK"] = "257"
        with self.assertRaisesRegex(ValueError, "must fit within OPEN_MAX"):
            BUILD.check_descriptor_configuration(config)
        # The POSIX floor in the pinned header is effective even below 16.
        config.update(CONFIG_NFILE_DESCRIPTORS_PER_BLOCK="16", CONFIG_LIBC_OPEN_MAX="8")
        BUILD.check_descriptor_configuration(config)
        config["CONFIG_NFILE_DESCRIPTORS_PER_BLOCK"] = "17"
        with self.assertRaisesRegex(ValueError, "must fit within OPEN_MAX"):
            BUILD.check_descriptor_configuration(config)

    def test_descriptor_configuration_refuses_missing_or_malformed_limits(self):
        good = {"CONFIG_NFILE_DESCRIPTORS_PER_BLOCK": "8", "CONFIG_LIBC_OPEN_MAX": "256"}
        for name in good:
            for invalid in (None, "", "-1", "y", "0x100"):
                with self.subTest(name=name, invalid=invalid):
                    config = dict(good)
                    if invalid is None:
                        del config[name]
                    else:
                        config[name] = invalid
                    with self.assertRaisesRegex(ValueError, "nonnegative decimal integer"):
                        BUILD.check_descriptor_configuration(config)
        with self.assertRaisesRegex(ValueError, "must fit within OPEN_MAX"):
            BUILD.check_descriptor_configuration(dict(good, CONFIG_NFILE_DESCRIPTORS_PER_BLOCK="0"))

    def test_encoded_descriptors_require_separate_ssh_qualification(self):
        config = {"CONFIG_NFILE_DESCRIPTORS_PER_BLOCK": "8", "CONFIG_LIBC_OPEN_MAX": "256",
                  "CONFIG_FDCHECK": "y"}
        with self.assertRaisesRegex(ValueError, "FDCHECK disabled"):
            BUILD.check_descriptor_configuration(config)
        config["CONFIG_FDCHECK"] = "n"
        BUILD.check_descriptor_configuration(config)

    def test_incomplete_build_cannot_emit_crossbuild_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            (out / "resolved.config").write_text("CONFIG_NSH_DISABLEBG=y\n")
            (out / "symbols.txt").write_text("00000000 T ssh_probe_main\n")
            for name in ("ssh-build-evidence.json", "ssh-dropbear-source-receipt.json",
                         "ssh-localoptions.h"):
                (out / name).write_text("stale-success-control\n")
            with self.assertRaisesRegex(ValueError, "required cross-build artifact"):
                BUILD.check(out, False)
            self.assertFalse((out / "ssh-build-evidence.json").exists())
            self.assertFalse((out / "ssh-dropbear-source-receipt.json").exists())
            self.assertFalse((out / "ssh-localoptions.h").exists())

    def test_secret_echo_across_chunks_is_withheld(self):
        secret = secrets.token_bytes(24)
        guard = SSH.SecretGuard((secret,))
        guard.feed(b"public output\r\n" + secret[:11])
        with self.assertRaisesRegex(SSH.CheckFailure, "secret_echo_detected_output_withheld") as raised:
            guard.feed(secret[11:] + b"\r\n")
        self.assertNotIn(repr(secret), str(raised.exception))

    def test_stale_sdk_receipt_cannot_emit_crossbuild_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            # Deliberately invalid host control inputs; no compiled image or
            # device success is synthesized. Validation must stop at the SDK.
            for name in ("resolved.config", "symbols.txt", "provenance.txt",
                         "std-main-thread-once.patch", "nuttx/nuttx",
                         "nuttx/nuttx.bin", "nuttx/nuttx.merged.bin"):
                path = out / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("invalid-host-control\n")
            (out / "std-patch.json").write_text('{}')
            with self.assertRaisesRegex(ValueError, "main-thread initialization fix"):
                BUILD.check(out, False)
            self.assertFalse((out / "ssh-build-evidence.json").exists())

    def test_secret_file_requires_private_owned_regular_file_and_no_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential"
            value = b"Fixture9!" + secrets.token_hex(8).encode()
            path.write_bytes(value + b"\n")
            path.chmod(0o600)
            self.assertEqual(SSH.password_file(path), value)
            path.chmod(0o644)
            with self.assertRaises(SSH.CheckFailure):
                SSH.password_file(path)
            path.chmod(0o600)
            alias = Path(directory) / "alias"
            alias.symlink_to(path)
            with self.assertRaises(OSError):
                SSH.password_file(alias)

    def test_missing_fixture_output_is_a_failed_control(self):
        token = secrets.token_hex(12)
        complete = b"\r\n".join(SSH.marker(name, token) for name in ("THREAD", "STDOUT", "STDERR", "SUCCESS"))
        SSH.require_stdio(complete, token)
        for name in ("THREAD", "STDOUT", "STDERR", "SUCCESS"):
            with self.assertRaisesRegex(SSH.CheckFailure, "missing_rust"):
                SSH.require_stdio(complete.replace(SSH.marker(name, token), b""), token)
        with self.assertRaises(SSH.CheckFailure):
            SSH.require_stdio(complete, secrets.token_hex(12))

    def test_leaked_task_or_descriptor_is_a_failed_control(self):
        baseline = {"tasks": 12, "fd_rows": 29}
        SSH.assert_resource_stability(baseline, dict(baseline))
        for key in baseline:
            after = dict(baseline)
            after[key] += 1
            with self.assertRaisesRegex(SSH.CheckFailure, "resource_growth"):
                SSH.assert_resource_stability(baseline, after)
        with self.assertRaisesRegex(SSH.CheckFailure, "resource_measurement_missing"):
            SSH.assert_resource_stability(baseline, {"tasks": 12})

    def test_stopped_state_without_child_reap_is_not_complete_shutdown(self):
        console = CONSOLE.SerialLab.__new__(CONSOLE.SerialLab)
        console.api = SSH
        console.starts, console.server_exits = 1, 0
        console.send = Mock()
        console.require_state = Mock()
        console.read_until = Mock(side_effect=[(b"", (b"0",)), (b"", ())])
        with self.assertRaisesRegex(SSH.CheckFailure, "native_child_was_not_reaped"):
            console.stop()

    def test_maintenance_commands_match_echo_before_prompt_and_expose_failed_stop(self):
        console = CONSOLE.SerialLab.__new__(CONSOLE.SerialLab)
        console.api, console.guard = SSH, SSH.SecretGuard(())
        console.pending = bytearray(b"\r\nnsh> \r\nnsh> ")
        console.event_line = bytearray()
        console.connection = Mock()
        console.connection.read.side_effect = [
            b"\x1b[Kifup wlan0\r\nup\r\nnsh> ",
            b"\x1b[Kecho $?\r\n0\r\nnsh> ",
            b"NXRS_SSH_LAB_CONTROL action=stop result=1\r\n",
        ]
        console.send = Mock()
        self.assertEqual(console.shell_status("ifup wlan0"), 0)
        with self.assertRaisesRegex(SSH.CheckFailure, "native_stop_failed:1"):
            console.stop()

    def test_secret_frame_fits_usb_receive_buffer(self):
        console = CONSOLE.SerialLab.__new__(CONSOLE.SerialLab)
        console.send = Mock()
        frame = bytes(range(180))
        with patch.object(CONSOLE.time, "sleep") as pause:
            console.send_secret_frame(frame)
        writes = [call.args[0] for call in console.send.call_args_list]
        self.assertEqual(b"".join(writes), frame)
        self.assertLess(max(map(len, writes)), 64)
        self.assertEqual(pause.call_count, len(writes) - 1)

    def test_real_procfs_descriptor_rows_and_incomplete_snapshot(self):
        # Formatting from pinned fs_procfsproc.c: %-3d %-7d %-4x %-9ld %-14s.
        raw = (b"NXRS_SSH_SNAPSHOT_BEGIN ticks=5\r\n"
               b"NXRS_SSH_TASKS count=2 ids=[0, 7]\r\n"
               b"NXRS_SSH_PROC path=/proc/7/stack\r\nStackSize: 65536\r\n"
               b"NXRS_SSH_PROC path=/proc/7/group/fd\r\n"
               b"FD  OFLAGS  TYPE POS       PATH\r\n"
               b"0   3       1    0         /dev/console\r\n"
               b"1   3       a    0         /dev/pts/0\r\n"
               b"NXRS_SSH_SNAPSHOT_END ticks=5\r\n")
        with tempfile.TemporaryDirectory() as directory:
            console = CONSOLE.SerialLab.__new__(CONSOLE.SerialLab)
            console.api, console.out = SSH, Path(directory)
            console.send = Mock()
            console.read_until = Mock(return_value=(raw, ()))
            self.assertEqual(console.snapshot("control"), {"tasks": 2, "fd_rows": 2, "ticks": 5})
            console.read_until = Mock(return_value=(raw.replace(b"NXRS_SSH_PROC path=/proc/7/stack", b"NXRS_SSH_PROC_UNAVAILABLE"), ()))
            with self.assertRaisesRegex(SSH.CheckFailure, "incomplete_fd_or_stack_evidence"):
                console.snapshot("invalid")
            self.assertFalse((Path(directory) / "invalid.txt").exists())

    def test_empty_or_failed_trials_cannot_be_saved_as_qualified(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SSH.OpenSsh("127.0.0.1", 2222, "test", "/tmp/known_hosts", b"Fixture9!")
            suite = SSH.Suite(client, Path(directory), evidence_class="host_control")
            suite.save()
            evidence = json.loads((Path(directory) / "summary.json").read_text())
            self.assertEqual(evidence["overall"], "not_run")
            def fail():
                raise SSH.CheckFailure("deliberate_missing_output")
            suite.case("host_control_not_a_device_run", fail)
            evidence = json.loads((Path(directory) / "summary.json").read_text())
            self.assertEqual(evidence["overall"], "failed")
            self.assertEqual(evidence["evidence_class"], "host_control")
            self.assertTrue(evidence["pending"])

    def test_actual_controlling_pty_keeps_password_out_of_argv_and_output(self):
        # A local helper validates terminal mechanics only, not SSH or NuttX.
        code = """
import sys, termios
before=termios.tcgetattr(0); quiet=termios.tcgetattr(0)
quiet[3] &= ~termios.ECHO
termios.tcsetattr(0,termios.TCSANOW,quiet)
print('Password: ',end='',flush=True)
value=sys.stdin.readline()
termios.tcsetattr(0,termios.TCSANOW,before)
print('\\nPTY_PASSWORD_READ bytes='+str(len(value.rstrip('\\n'))),flush=True)
sys.exit(7)
"""
        value = b"Fixture9!" + secrets.token_hex(8).encode()
        client = SSH.OpenSsh("127.0.0.1", 2222, "test", "/tmp/known_hosts", value)
        process = SSH.PtyProcess([sys.executable, "-c", code], (value,))
        try:
            client.password_prompt(process)
            process.expect(f"PTY_PASSWORD_READ bytes={len(value)}".encode())
            self.assertEqual(process.wait(), 7)
            self.assertNotIn(value, process.pending)
        finally:
            process.close()

    def test_echo_enabled_password_prompt_is_refused_before_sending(self):
        code = "print('Password: ',end='',flush=True); input()"
        value = b"Fixture9!" + secrets.token_hex(8).encode()
        client = SSH.OpenSsh("127.0.0.1", 2222, "test", "/tmp/known_hosts", value)
        process = SSH.PtyProcess([sys.executable, "-c", code], (value,))
        try:
            with self.assertRaisesRegex(SSH.CheckFailure, "echo_enabled"):
                client.password_prompt(process)
        finally:
            process.close()

    @unittest.skipUnless(shutil.which("ssh"), "OpenSSH client unavailable")
    def test_real_openssh_effective_configuration_is_strict_and_password_only(self):
        value = b"Fixture9!" + secrets.token_hex(8).encode()
        client = SSH.OpenSsh("127.0.0.1", 2222, "test", "/tmp/nxrs_known_hosts", value)
        arguments = client.arguments()
        self.assertNotIn(value.decode(), " ".join(arguments))
        result = subprocess.run([arguments[0], "-G", *arguments[1:]], capture_output=True, text=True, check=True)
        config = dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)
        self.assertEqual(config["stricthostkeychecking"], "true")
        self.assertEqual(config["pubkeyauthentication"], "false")
        self.assertEqual(config["kbdinteractiveauthentication"], "no")
        self.assertEqual(config["passwordauthentication"], "yes")
        self.assertEqual(config["identityagent"], "none")
        self.assertEqual(config["controlmaster"], "false")

    @unittest.skipUnless(shutil.which("ssh-keygen"), "OpenSSH key tool unavailable")
    def test_actual_public_key_fingerprint_validation_rejects_changed_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "host"
            subprocess.run(["ssh-keygen", "-q", "-t", "ecdsa", "-b", "256", "-N", "", "-f", str(key)], check=True)
            fingerprint = subprocess.check_output(["ssh-keygen", "-l", "-E", "sha256", "-f", str(key)]).decode().split()[1]
            public = key.with_suffix(".pub").read_text().split()
            known = Path(directory) / "known_hosts"
            known.write_text(f"[127.0.0.1]:2222 {public[0]} {public[1]}\n")
            self.assertEqual(SSH.verify_known_hosts(known, "127.0.0.1", 2222, fingerprint), known)
            with self.assertRaisesRegex(SSH.CheckFailure, "fingerprint_mismatch"):
                SSH.verify_known_hosts(known, "127.0.0.1", 2222, "SHA256:deliberately-wrong")


@unittest.skipUnless(shutil.which("git") and shutil.which("patch"), "Git and patch required")
class DropbearSourceReceiptTests(unittest.TestCase):
    """Verify real full-series source preparation without synthesizing an image."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="nxrs-ssh-source-control-")
        self.addCleanup(directory.cleanup)
        self.out = Path(directory.name)
        self.upstream = NATIVE.prepare(self.out / "apps")
        self.port = self.out / "apps/netutils/dropbear"
        self.expected = json.loads(BUILD.DROPBEAR_SOURCE_RECEIPT.read_text())
        self.revision = self.expected["revision"]

    def test_real_complete_patch_sequence_matches_without_claiming_a_crossbuild(self):
        receipt = BUILD.check_dropbear_sources(self.out, self.revision)
        self.assertEqual(receipt["evidence_class"], "prepared_source_identity")
        self.assertEqual(receipt["expected_receipt"], self.expected)
        self.assertEqual(set(receipt["recipe_sha256"]), {"Makefile", "CMakeLists.txt"})
        self.assertEqual(receipt["device_qualification"], "pending")
        self.assertFalse((self.out / "ssh-build-evidence.json").exists())

    def test_historical_misplaced_hooks_fail_even_with_correct_patch_bytes(self):
        # The old zero-context patch exits successfully after patches 0001-0004,
        # but lands a cancellation hook in a comment. Keep the corrected patch
        # files in the tree: validation must inspect actual prepared C source.
        bad_out = self.out / "historical"
        bad_upstream = NATIVE.prepare(
            bad_out / "apps", final_patch=NATIVE.FIXTURE / "misplaced-zero-context.patch",
            verify_prepared=False)
        self.assertIn("dropbear_nuttx_channel_cancel(channel);",
                      (bad_upstream / "src/common-channel.c").read_text())
        with self.assertRaisesRegex(ValueError, "prepared Dropbear source differs: src/common-channel.c"):
            BUILD.check_dropbear_sources(bad_out, self.revision)
        self.assertFalse((bad_out / "ssh-build-evidence.json").exists())

    def test_both_recipes_must_declare_the_complete_ordered_sequence(self):
        first, second = [entry["path"].removeprefix("patch/")
                         for entry in self.expected["nested_patches"][:2]]
        for name in ("Makefile", "CMakeLists.txt"):
            path = self.port / name
            original = path.read_text()
            for changed in (original.replace(first, "omitted.patch"),
                            original.replace(first, "SWAP_PLACEHOLDER")
                            .replace(second, first).replace("SWAP_PLACEHOLDER", second)):
                with self.subTest(recipe=name, changed=changed != original):
                    path.write_text(changed)
                    with self.assertRaisesRegex(ValueError, "nested patch sequence differs"):
                        BUILD.check_dropbear_sources(self.out, self.revision)
                    path.write_text(original)

    def test_missing_or_changed_nested_patch_is_rejected(self):
        path = self.port / self.expected["nested_patches"][-1]["path"]
        original = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(ValueError, "prepared Dropbear patch missing"):
            BUILD.check_dropbear_sources(self.out, self.revision)
        path.write_bytes(original + b"\n# unexpected patch drift\n")
        with self.assertRaisesRegex(ValueError, "prepared Dropbear patch differs"):
            BUILD.check_dropbear_sources(self.out, self.revision)

    def test_missing_or_changed_prepared_source_is_rejected(self):
        path = self.upstream / "src/common-channel.c"
        original = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(ValueError, "prepared Dropbear source missing"):
            BUILD.check_dropbear_sources(self.out, self.revision)
        path.write_bytes(original + b"\n/* unexpected source drift */\n")
        with self.assertRaisesRegex(ValueError, "prepared Dropbear source differs"):
            BUILD.check_dropbear_sources(self.out, self.revision)

    def test_configuration_must_select_the_receipted_upstream_revision(self):
        with self.assertRaisesRegex(ValueError, "revision differs from source receipt"):
            BUILD.check_dropbear_sources(self.out, "0" * 40)


@unittest.skipUnless(shutil.which("rustc"), "Rust host compiler unavailable")
class RustSshProbeHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.directory.name) / "ssh_probe"
        subprocess.run(["rustc", "--edition=2021", str(DIRECTORY / "src/main.rs"), "-o", str(cls.binary)], check=True)
        cls.console_binary = Path(cls.directory.name) / "owned-console"
        source = Path(cls.directory.name) / "owned-console.rs"
        source.write_text('''
use std::io::{Read, Write};
#[path = ''' + json.dumps(str(DIRECTORY / "src/console.rs")) + ''']
mod local;
fn main() {
    let mut console = local::Console::stdio().unwrap();
    let (ready_tx, ready_rx) = std::sync::mpsc::channel();
    let (done_tx, done_rx) = std::sync::mpsc::channel();
    let holder = std::thread::spawn(move || {
        let _stdin = std::io::stdin().lock();
        let _stdout = std::io::stdout().lock();
        let _stderr = std::io::stderr().lock();
        ready_tx.send(()).unwrap();
        done_rx.recv_timeout(std::time::Duration::from_secs(10)).unwrap();
    });
    ready_rx.recv().unwrap();
    writeln!(console.output, "LOCAL_READY").unwrap();
    assert_eq!(console.line().unwrap(), "first");
    assert_eq!(console.line().unwrap(), "second");
    let mut header = [0; 7];
    console.input.read_exact(&mut header).unwrap();
    assert_eq!(&header, b"HEADER7");
    let mut body = [0; 8];
    console.input.read_exact(&mut body).unwrap();
    assert_eq!(&body, b"payload\\n");
    writeln!(console.error, "LOCAL_COMPLETE").unwrap();
    done_tx.send(()).unwrap();
    holder.join().unwrap();
}
''')
        subprocess.run(["rustc", "--edition=2021", str(source), "-o", str(cls.console_binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_ordinary_stdio_thread_and_failure_contract(self):
        token = secrets.token_hex(12)
        result = subprocess.run([str(self.binary), "stdio", token], input=f"input:{token}\n", capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        SSH.require_stdio((result.stdout + result.stderr).encode(), token)
        result = subprocess.run([str(self.binary), "fail", token], capture_output=True)
        self.assertEqual(result.returncode, 7)
        self.assertIn(SSH.marker("EXPECTED_ERROR", token), result.stderr)
        result = subprocess.run([str(self.binary), "stdio", token], input=b"wrong\n", capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_large_output_sequences_and_invalid_count(self):
        token = secrets.token_hex(12)
        result = subprocess.run([str(self.binary), "bulk", token, "1024"], capture_output=True, check=True)
        lines = result.stdout.splitlines()
        for index in range(1024):
            self.assertEqual(lines[index], SSH.marker("DATA", token) + f" sequence={index:05}".encode())
        self.assertEqual(lines[-1], SSH.marker("BULK_DONE", token) + b" lines=1024")
        result = subprocess.run([str(self.binary), "bulk", token, "16385"], capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_local_console_bypasses_global_stdio_locks_and_read_ahead(self):
        process = SSH.PtyProcess([str(self.console_binary)])
        try:
            process.expect(b"LOCAL_READY", 2)
            # Deliver multiple lines and a framed payload together. Unbuffered
            # owned handles must leave each following byte available to reads.
            process.send(b"first\nsecond\nHEADER7payload\n")
            process.expect(b"LOCAL_COMPLETE", 2)
            self.assertEqual(process.wait(), 0)
        finally:
            process.close()


if __name__ == "__main__":
    unittest.main()
