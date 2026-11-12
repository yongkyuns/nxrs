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
BOUNDED_PATCH = "patch/0005-nuttx-bounded-service.patch"
BACKPORTS = (
    "patch/0006-limit-kex-deferred-replies.patch",
    "patch/0007-validate-ecdsa-curve-identifier.patch",
    "patch/0008-enforce-channel-packet-bounds.patch",
)


def prepare(destination, *, final_patch=None, omitted_backport=None, verify_prepared=True):
    """Apply the complete firmware patch series to checked upstream bytes.

    Overrides are only for compiled historical negative controls. They still
    verify every input and patch; normal preparation verifies every postimage.
    """
    destination.mkdir(parents=True, exist_ok=True)
    metadata = json.loads((FIXTURE / "upstream.json").read_text())
    receipt = json.loads(RECEIPT.read_text())
    if omitted_backport is not None:
        if omitted_backport not in BACKPORTS or verify_prepared:
            raise AssertionError("Only an explicit negative control may omit a backport")
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
    if metadata["files"].keys() & metadata.get("support_files", {}).keys():
        raise AssertionError("Patched and auxiliary source inputs must be distinct")
    for name, entry in {**metadata["files"], **metadata.get("support_files", {})}.items():
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
        if index >= 4:
            options = ["--fuzz=0"]
        if entry["path"] == BOUNDED_PATCH and final_patch is not None:
            selected = final_patch
            options = []
        if entry["path"] == omitted_backport:
            continue
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

    def extract_functions(self, root, source_path, declarations, filename, *, append=False):
        source = (root / "netutils/dropbear/dropbear" / source_path).read_text()
        functions = []
        for declaration in declarations:
            match = re.search(r"(?ms)^" + declaration + r"\([^;{}]*\)\s*\{.*?^\}", source)
            self.assertIsNotNone(match, declaration)
            functions.append(match.group())
        with (root / filename).open("a" if append else "w") as target:
            target.write("\n\n".join(functions) + "\n")

    def extract_channel_functions(self, root, declarations, filename):
        self.extract_functions(root, "src/common-channel.c", declarations, filename)

    def extract_backport_limits(self, root):
        # The negative controls retain the corrected profile's actual constants
        # while compiling historical function bodies. No C implementation is
        # reconstructed in Python, and the witnesses assert the admitted bound.
        source = (self.upstream / "src/sysoptions.h").read_text()
        blocks = []
        for macro, next_macro in (("MAX_NAME_LEN", "MAX_PROPOSED_ALGO"),
                                  ("MAX_CHANNELS", "MAX_STRING_LEN")):
            start = source.index("#define " + macro + " ")
            end = source.index("#define " + next_macro + " ", start)
            blocks.append(source[start:end])
        native_limit = [block for block in re.findall(
            r"(?ms)^#ifdef DROPBEAR_NUTTX\n.*?^#endif$", source)
                       if "#define MAX_DEFER_REPLY_QUEUE " in block]
        self.assertEqual(len(native_limit), 1)
        blocks.extend(native_limit)
        defaults = (self.upstream / "src/default_options.h").read_text()
        payload_limit = re.findall(r"(?m)^#define TRANS_MAX_PAYLOAD_LEN .+$", defaults)
        self.assertEqual(len(payload_limit), 1)
        blocks.extend(payload_limit)
        (root / "backport-limits.inc").write_text("\n".join(blocks) + "\n")

    def prepare_negative_control(self, name, omitted_backport):
        root = self.root / name
        prepare(root, omitted_backport=omitted_backport, verify_prepared=False)
        shutil.copytree(FIXTURE / "include", root / "include")
        return root

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

    def prepare_deferred_queue_witness(self, root):
        self.extract_backport_limits(root)
        self.extract_functions(root, "src/common-session.c", (r"void common_session_init",),
                               "deferred-queue-functions.inc")
        self.extract_functions(root, "src/packet.c",
                               (r"static void enqueue_reply_packet",
                                r"void maybe_flush_reply_queue"),
                               "deferred-queue-functions.inc", append=True)

    def test_deferred_replies_are_bounded_before_allocation_and_reset(self):
        self.prepare_deferred_queue_witness(self.root)
        self.compile_run("deferred_queue")

    def test_historical_deferred_queue_fails_each_bound_and_reset_control(self):
        root = self.prepare_negative_control("unbounded-queue", BACKPORTS[0])
        self.prepare_deferred_queue_witness(root)
        controls = ((1, "deferred reply allocated beyond bound"),
                    (2, "drain retained deferred reply count"),
                    (3, "new session retained deferred count"))
        for case, failure in controls:
            with self.subTest(case=case):
                self.compile_run("deferred_queue", "-DWITNESS_QUEUE_CASE=" + str(case),
                                 root=root, expected_failure=failure)

    def test_ecdsa_helper_refuses_short_or_mismatched_curve_identifiers(self):
        self.extract_functions(self.root, "src/ecdsa.c",
                               (r"ecc_key \*buf_get_ecdsa_pub_key",),
                               "ecdsa-identifier-function.inc")
        self.compile_run("ecdsa_identifier")

    def test_historical_ecdsa_helper_overread_is_detected_by_address_sanitizer(self):
        # Invoke the complete helper directly. The host-key dispatcher normally
        # rejects malformed outer key names before reaching it; this is parser
        # hardening coverage, not evidence of remote host-key exploitability.
        root = self.prepare_negative_control("short-curve-identifier", BACKPORTS[1])
        self.extract_functions(root, "src/ecdsa.c",
                               (r"ecc_key \*buf_get_ecdsa_pub_key",),
                               "ecdsa-identifier-function.inc")
        self.compile_run("ecdsa_identifier", root=root,
                         expected_failure="AddressSanitizer: heap-buffer-overflow")

    def prepare_channel_packet_witness(self, root):
        self.extract_backport_limits(root)
        self.extract_channel_functions(root,
                                       (r"void recv_msg_channel_open",
                                        r"void recv_msg_channel_open_confirmation"),
                                       "channel-packet-functions.inc")

    def test_both_channel_open_handlers_bound_peer_packet_sizes(self):
        self.prepare_channel_packet_witness(self.root)
        self.compile_run("channel_packet")

    def test_historical_channel_packet_sizes_fail_each_admission_control(self):
        root = self.prepare_negative_control("channel-packet-size", BACKPORTS[2])
        self.prepare_channel_packet_witness(root)
        for case in (1, 2, 4):
            with self.subTest(case=case):
                self.compile_run("channel_packet", "-DWITNESS_CHANNEL_CASE=" + str(case),
                                 root=root,
                                 expected_failure="selected->transmaxpacket == expected_packet")

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
