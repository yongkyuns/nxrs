"""Run pinned NuttX poll-thread lifetime code; no radio or DMA emulation."""
import hashlib
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "drivers/net/netdev_upperhalf.c"
PATCH = ROOT / "upstream/nuttx/patches/0101-netdev-poll-thread-lifetime.patch"


def function(source, name):
    match = re.search(rf"(?m)^(?:static )?(?:int|void) {name}\(", source)
    if not match:
        raise AssertionError(f"missing actual NuttX function: {name}")
    opening = source.index("{", match.start())
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[match.start():end] + "\n"


class NetdevLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("cc")
        if not compiler:
            raise unittest.SkipTest("host C compiler required")
        cls.temporary = tempfile.TemporaryDirectory(prefix="nxrs-netdev-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        revision = subprocess.check_output(
            ["git", "rev-parse", ":external/nuttx"], cwd=ROOT, text=True).strip()

        def pinned(path):
            return subprocess.check_output(
                ["git", "-C", str(ROOT / "external/nuttx"), "show", f"{revision}:{path}"],
                text=True)

        original = pinned(SOURCE)
        if hashlib.sha256(original.encode()).hexdigest() != "0ffb4bf9be5ba5ce1c2586b2af88302eace9661c3559c8420d392a7f7bc1908e":
            raise AssertionError("poll-thread fixture requires the pinned NuttX source")
        series = runpy.run_path(str(ROOT / "tools/apply-nuttx-patches.py"))["SERIES"]["nuttx"]
        if series.count(PATCH.name) != 1:
            raise AssertionError("tested patch must occur once in the firmware series")
        path = cls.directory / SOURCE
        path.parent.mkdir(parents=True)
        path.write_text(original)
        for name in ("include/nuttx/net/net.h", "net/utils/utils.h"):
            copied = cls.directory / name
            copied.parent.mkdir(parents=True, exist_ok=True)
            copied.write_text(pinned(name))
        subprocess.run(["patch", "--batch", "-p1", "-i", str(PATCH)],
                       cwd=cls.directory, check=True, capture_output=True)
        mutex = pinned("libs/libc/misc/lib_mutex.c")
        network = pinned("net/utils/net_lock.c")
        ioctl = pinned("net/netdev/netdev_ioctl.c")
        locks = "".join(function(mutex, name) for name in
                        ("nxrmutex_lock", "nxrmutex_unlock", "nxrmutex_breaklock", "nxrmutex_restorelock"))
        locks += "".join(function(network, name) for name in
                         ("net_lock", "net_unlock", "net_breaklock", "net_restorelock"))
        (cls.directory / "locks.inc").write_text(locks)
        cls.executables = []
        for label, source in (("original", original), ("patched", path.read_text())):
            types = source[source.index("struct netdev_thread_s\n"):
                           source.index("/****************************************************************************\n * Private Function Prototypes")]
            code = types + "".join(function(source, name) for name in
                                   ("netdev_upper_rxpoll_work", "netdev_upper_work", "netdev_upper_loop",
                                    "netdev_upper_exit_thread", "netdev_upper_ifup", "netdev_upper_ifdown",
                                    "netdev_lower_unregister"))
            code += "".join(function(ioctl, name) for name in ("netdev_ifup", "netdev_ifdown"))
            (cls.directory / "kernel.inc").write_text(code)
            executable = cls.directory / label
            built = subprocess.run([compiler, "-std=gnu11", "-pthread", "-Wall", "-Wextra",
                                    "-Werror", "-Wno-unused-parameter", "-Wno-implicit-fallthrough",
                                    "-I", str(cls.directory), str(ROOT / "tests/host/netdev_lifecycle.c"),
                                    "-o", str(executable)], capture_output=True, text=True)
            if built.returncode:
                raise AssertionError(built.stderr)
            cls.executables.append(executable)

    def witness(self, case):
        before = subprocess.run([str(self.executables[0]), case], capture_output=True,
                                text=True, timeout=8)
        after = subprocess.run([str(self.executables[1]), case], capture_output=True,
                               text=True, timeout=8)
        self.assertNotEqual(before.returncode, 0, "original kernel must reproduce this regression")
        self.assertEqual(after.returncode, 0, after.stderr)

    def test_canceled_join_retry_and_unregister_ownership(self):
        self.witness("cancel")

    def test_failed_start_partial_rss_and_reap_before_replacement(self):
        self.witness("startup")

    def test_contended_join_admission_and_unregister_order(self):
        self.witness("contended")


if __name__ == "__main__":
    unittest.main()
