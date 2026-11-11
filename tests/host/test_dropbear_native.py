"""Compile the real patched Dropbear port against small native ABI witnesses.

The fixture contains checksum-verified upstream sources so this suite
runs offline without a submodule checkout. No SSH parser or crypto algorithm is
reimplemented by the witness. Protocol dependencies are stubbed only in lifecycle
unit tests; actual on-target SSH qualification is a separate gate.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/host/fixtures/dropbear-native"
PATCH = ROOT / "upstream/nuttx-apps/patches/0005-dropbear-bounded-service.patch"


def prepare(destination):
    metadata = json.loads((FIXTURE / "upstream.json").read_text())
    patch = PATCH.read_text()
    for section in re.split(r"(?m)^diff --git ", patch)[1:]:
        lines = section.splitlines()
        old = next(line[4:] for line in lines if line.startswith("--- "))
        if old == "/dev/null":
            continue
        path = old[2:]
        hunks = [i for i, line in enumerate(lines) if line.startswith("@@ ")]
        if len(hunks) != 1 or not lines[hunks[0]].startswith("@@ -1,"):
            raise AssertionError("Port fixture requires a complete pinned preimage: " + path)
        content = "\n".join(line[1:] for line in lines[hunks[0] + 1:]
                            if line.startswith((" ", "-"))) + "\n"
        if hashlib.sha256(content.encode()).hexdigest() != metadata["apps_preimages"][path]:
            raise AssertionError("Port preimage differs from pinned source: " + path)
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(destination.parent)}
    subprocess.run(["git", "apply", "--whitespace=error", str(PATCH)],
                   cwd=destination, env=env, check=True, capture_output=True)
    upstream = destination / "netutils/dropbear/dropbear"
    for name, entry in metadata["files"].items():
        content = (FIXTURE / "upstream" / name).read_bytes()
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise AssertionError("Upstream fixture checksum mismatch: " + name)
        target = upstream / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    for patch_name in ("0002-use-nuttx-passwd-auth.patch", "0005-nuttx-bounded-service.patch"):
        subprocess.run(["patch", "-s", "-N", "-l", "-p1", "-i",
                        str(destination / "netutils/dropbear/patch" / patch_name)],
                       cwd=upstream, check=True, capture_output=True)
    return upstream


class DropbearNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiler = shutil.which("cc") or shutil.which("clang")
        if not cls.compiler:
            raise unittest.SkipTest("host C compiler required")
        series = json.loads((ROOT / "upstream/build-patches.json").read_text())
        if PATCH.name not in series["nuttx-apps"]["patches"]:
            raise AssertionError("The tested native patch is not in the firmware build")
        cls.temporary = tempfile.TemporaryDirectory(prefix="nxrs-dropbear-native-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.upstream = prepare(cls.root)
        shutil.copytree(FIXTURE / "include", cls.root / "include")

    def compile_run(self, witness, *extra):
        binary = self.root / (witness + "-" + str(len(extra)))
        command = [self.compiler, "-std=c11", "-D_GNU_SOURCE", "-O1", "-g",
                   "-Wall", "-Wextra", "-Werror", "-ffunction-sections", "-fdata-sections",
                   "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                   "-I" + str(self.root / "include"), "-I" + str(self.root),
                   "-I" + str(self.root / "netutils/dropbear/port"),
                   "-I" + str(self.upstream), *extra,
                   str(FIXTURE / (witness + ".c")), "-pthread", "-Wl,--gc-sections",
                   "-o", str(binary)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0:abort_on_error=1",
                                     "UBSAN_OPTIONS": "halt_on_error=1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_foreground_singleton_listener_and_monotonic_stop(self):
        import socket
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        self.compile_run("lifecycle", "-DCONFIG_NETUTILS_DROPBEAR_PORT=" + str(port))

    def test_qualified_entropy_refuses_bad_or_unready_sources(self):
        self.compile_run("entropy")
        self.compile_run("entropy", "-DUNQUALIFIED_SOURCE")

    def test_random_seeding_failure_and_restart_cleanup(self):
        self.compile_run("random")

    def test_password_handler_rejects_embedded_nul_and_empty_payloads(self):
        self.compile_run("authentication")

    def test_hmac_io_failure_releases_session_once(self):
        self.compile_run("hmac")

    def test_failed_realloc_remains_owned_by_its_epoch(self):
        self.compile_run("allocation")

    def test_owned_nsh_waiter_cancellation_and_exit_status(self):
        self.compile_run("nsh")

    def test_channel_limit_and_client_close_reach_the_session_owner(self):
        # These complete definitions are compiled verbatim from the prepared
        # pinned source. Avoid recreating the guard or close policy in Python.
        source = (self.upstream / "src/common-channel.c").read_text()
        functions = []
        for declaration in (r"static struct Channel\* newchannel",
                            r"void recv_msg_channel_close"):
            match = re.search(r"(?ms)^" + declaration + r"\(.*?^\}", source)
            self.assertIsNotNone(match, declaration)
            functions.append(match.group())
        (self.root / "channel-functions.inc").write_text("\n\n".join(functions) + "\n")
        self.compile_run("channel")


if __name__ == "__main__":
    unittest.main()
