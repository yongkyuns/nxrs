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
RECEIPT = ROOT / "upstream/nuttx-apps/dropbear-source-receipt.json"


def prepare(destination, *, final_patch=None, verify_prepared=True):
    """Apply the complete firmware patch series to checked upstream bytes.

    The explicit override is only for the historical misapplication control;
    normal witnesses and the build verifier use the checked receipt unchanged.
    """
    destination.mkdir(parents=True, exist_ok=True)
    metadata = json.loads((FIXTURE / "upstream.json").read_text())
    receipt = json.loads(RECEIPT.read_text())
    if (receipt["revision"] != metadata["revision"] or
            receipt["nuttx_apps_revision"] != metadata["nuttx_apps_revision"]):
        raise AssertionError("Dropbear source receipt revision mismatch")
    for name, entry in metadata["apps_fixtures"].items():
        content = (FIXTURE / "apps" / name).read_bytes()
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise AssertionError("Apps fixture checksum mismatch: " + name)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
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
    touched = set()
    patch_entries = receipt["nested_patches"]
    for index, entry in enumerate(patch_entries):
        selected = destination / "netutils/dropbear" / entry["path"]
        if hashlib.sha256(selected.read_bytes()).hexdigest() != entry["sha256"]:
            raise AssertionError("Nested patch checksum mismatch: " + entry["path"])
        touched.update(re.findall(r"(?m)^\+\+\+ b/(.+)$", selected.read_text()))
        options = []
        if index == len(patch_entries) - 1:
            if final_patch is not None:
                selected = final_patch
            else:
                options = ["--fuzz=0"]
        subprocess.run(["patch", "-s", "-N", "-l", *options, "-p1", "-i",
                        str(selected)],
                       cwd=upstream, check=True, capture_output=True)
    expected_files = {entry["path"]: entry for entry in receipt["files"]}
    if touched != expected_files.keys() or touched != metadata["files"].keys():
        raise AssertionError("Source receipt must cover the complete nested patch series")
    for name, entry in expected_files.items():
        if entry["pristine_sha256"] != metadata["files"][name]["sha256"]:
            raise AssertionError("Pristine source receipt mismatch: " + name)
        if verify_prepared and hashlib.sha256((upstream / name).read_bytes()).hexdigest() != entry["prepared_sha256"]:
            raise AssertionError("Prepared Dropbear source mismatch: " + name)
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

    def compile_run(self, witness, *extra, root=None, expected_failure=None):
        root = root or self.root
        upstream = root / "netutils/dropbear/dropbear"
        binary = root / (witness + "-" + str(len(extra)))
        command = [self.compiler, "-std=c11", "-D_GNU_SOURCE", "-O1", "-g",
                   "-Wall", "-Wextra", "-Werror", "-ffunction-sections", "-fdata-sections",
                   "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                   "-I" + str(root / "include"), "-I" + str(root),
                   "-I" + str(root / "netutils/dropbear/port"),
                   "-I" + str(upstream), *extra,
                   str(FIXTURE / (witness + ".c")), "-pthread", "-Wl,--gc-sections",
                   "-o", str(binary)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0:abort_on_error=1",
                                     "UBSAN_OPTIONS": "halt_on_error=1"})
        if expected_failure is None:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, "Historical broken source unexpectedly passed")
            self.assertIn(expected_failure, result.stderr)

    def extract_channel_functions(self, root, declarations, filename):
        source = (root / "netutils/dropbear/dropbear/src/common-channel.c").read_text()
        functions = []
        for declaration in declarations:
            match = re.search(r"(?ms)^" + declaration + r"\([^;{}]*\)\s*\{.*?^\}", source)
            self.assertIsNotNone(match, declaration)
            functions.append(match.group())
        (root / filename).write_text("\n\n".join(functions) + "\n")

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
        self.extract_channel_functions(self.root,
                                       (r"static struct Channel\* newchannel",
                                        r"void recv_msg_channel_close"),
                                       "channel-functions.inc")
        self.compile_run("channel")

    def test_channel_removal_cancels_before_closing_or_releasing_resources(self):
        self.extract_channel_functions(self.root, (r"static void remove_channel",),
                                       "channel-removal-function.inc")
        self.compile_run("channel_cleanup")

    def test_historical_zero_context_patch_misplaces_live_cancellation(self):
        # The real old patch returns success after the full prerequisite series,
        # but inserts cancellation inside a comment. Compile the resulting actual
        # remove_channel function and require the behavioral witness to fail.
        metadata = json.loads((FIXTURE / "upstream.json").read_text())
        historical = FIXTURE / "misplaced-zero-context.patch"
        self.assertEqual(hashlib.sha256(historical.read_bytes()).hexdigest(),
                         metadata["misplaced_patch_sha256"])
        root = self.root / "historical"
        prepare(root, final_patch=historical, verify_prepared=False)
        shutil.copytree(FIXTURE / "include", root / "include")
        self.extract_channel_functions(root, (r"static void remove_channel",),
                                       "channel-removal-function.inc")
        self.compile_run("channel_cleanup", root=root,
                         expected_failure="cancellation_count == 1")


if __name__ == "__main__":
    unittest.main()
