"""Fail-closed checks for both pinned NuttX upstream patch series."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/apply-nuttx-patches.py"
SERIES = {
    "nuttx": ("libs/libc/tls/Kconfig", ROOT / "upstream/nuttx/patches"),
    "nuttx-apps": ("wireless/bluetooth/nimble/Makefile.nimble",
                   ROOT / "upstream/nuttx-apps/patches"),
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PatchSeriesTests(unittest.TestCase):
    def setUp(self):
        # Archive-like copies nested inside nxrs must not inherit its Git root.
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def fixture(self, component):
        marker, patch_dir = SERIES[component]
        upstream = ROOT / "external" / component
        source = self.base / component
        source.mkdir()
        patches = sorted(patch_dir.glob("*.patch"))
        self.assertTrue(patches)
        names = {marker}
        for patch in patches:
            names.update(line.split("\t", 2)[2] for line in subprocess.check_output(
                ["git", "apply", "--numstat", str(patch)], text=True,
            ).splitlines())
        original = {}
        for name in names:
            if not (upstream / name).is_file():
                continue  # New files must be created by the series.
            destination = source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(upstream / name, destination)
            original[name] = digest(destination)
        return source, upstream, patches, original

    def apply(self, component, source):
        return subprocess.run(
            ["python3", str(TOOL), "--component", component,
             "--source", str(source), "--revision", "test-revision",
             "--record", str(self.base / f"{component}-patches.json")],
            text=True, capture_output=True,
        )

    def test_both_series_apply_to_copies_and_record_provenance(self):
        for component in SERIES:
            with self.subTest(component=component):
                source, upstream, patches, original = self.fixture(component)
                result = self.apply(component, source)
                self.assertEqual(result.returncode, 0, result.stderr)
                ledger = json.loads((self.base / f"{component}-patches.json").read_text())
                self.assertEqual(ledger["component"], component)
                self.assertEqual(ledger["upstream_revision"], "test-revision")
                self.assertEqual(len(ledger["patches"]), len(patches))
                latest = {}
                for entry, patch in zip(ledger["patches"], patches):
                    self.assertEqual(entry["name"], patch.name)
                    self.assertEqual(entry["sha256"], digest(patch))
                    for name, hashes in entry["files"].items():
                        self.assertEqual(hashes["before"], latest.get(name, original.get(name)))
                        latest[name] = hashes["after"]
                for name, after in latest.items():
                    self.assertEqual(after, digest(source / name))
                for name, before in original.items():
                    self.assertEqual(digest(upstream / name), before)
                if component == "nuttx":
                    self.assertTrue((source / "arch/xtensa/src/esp32s3/esp32s3_camera.c").is_file())
                    self.assertIn("config TLS_GLOBAL_KEYS", (source / "libs/libc/tls/Kconfig").read_text())
                    self.assertIn("return -EINVAL;", (source / "arch/xtensa/src/esp32s3/esp32s3_spiram.c").read_text())
                    self.assertTrue((source / "boards/xtensa/esp32s3/esp32s3-devkit/src/esp32s3_freenove_userled.c").is_file())
                    transport = (source / "arch/arm/src/rp23xx/rp23xx_cyw43439.c").read_text()
                    self.assertIn("#include <nuttx/debug.h>", transport)
                    self.assertIn("set_errno(-err);", transport)
                    self.assertNotIn("set_errno(err);", transport)

    def test_rejects_reapplication_without_mutation(self):
        source, _, _, _ = self.fixture("nuttx")
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        witness = source / "arch/xtensa/src/esp32s3/esp32s3_ble.c"
        before = digest(witness)
        result = self.apply("nuttx", source)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already applied", result.stderr)
        self.assertEqual(digest(witness), before)

    def test_bcmf_open_network_reset_and_wpa2_staging(self):
        source, _, _, _ = self.fixture("nuttx")
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        text = (source / "drivers/wireless/ieee80211/bcm43xxx/bcmf_driver.c").read_text()
        start = text.index("int bcmf_wl_set_encode_ext(")
        end = text.index("\n/****************************************************************************", start)
        (self.base / "bcmf-function.h").write_text(text[start:end])
        executable = self.base / "bcmf-test"
        compiled = subprocess.run(["cc", "-std=c99", "-Wall", "-Wextra", "-Werror",
                        "-I", str(self.base), str(ROOT / "tests/nuttx-std/bcmf-encoding.c"),
                        "-o", str(executable)], capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        subprocess.run([str(executable)], check=True, capture_output=True, text=True)

    def test_rejects_incompatible_source_without_mutation(self):
        source, _, _, _ = self.fixture("nuttx")
        witness = source / "arch/xtensa/src/esp32s3/esp32s3_ble.c"
        witness.write_text(witness.read_text().replace(
            "#include <nuttx/wqueue.h>", "#include <nuttx/different.h>"))
        before = digest(witness)
        result = self.apply("nuttx", source)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("incompatible", result.stderr)
        self.assertEqual(digest(witness), before)
        self.assertFalse((self.base / "nuttx-patches.json").exists())

    def test_bcmf_gspi_receive_buffer_ownership(self):
        source, _, _, _ = self.fixture("nuttx")
        receive = source / "drivers/wireless/ieee80211/bcm43xxx/bcmf_gspi_f2_frame.c"
        baseline = receive.read_text()
        self.assertEqual(self.apply("nuttx", source).returncode, 0)

        def section(text, start):
            begin = text.index(start)
            end = text.index("\n/****************************************************************************", begin)
            return text[begin:end]

        for label, text, flags in (("baseline", baseline, ["-DEXPECT_BASELINE_BUGS"]),
                                   ("patched", receive.read_text(), [])):
            with self.subTest(receive=label):
                (self.base / "bcmf-gspi-header.h").write_text(
                    section(text, "begin_packed_struct struct f2_frame_header_s"))
                (self.base / "bcmf-gspi-functions.h").write_text(
                    section(text, "int process_f2_frame_header(") + "\n" +
                    section(text, "int bcmf_gspi_read_f2_frame("))
                executable = self.base / f"bcmf-gspi-{label}"
                result = subprocess.run([
                    "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-Wno-sign-compare", *flags, "-I", str(self.base),
                    str(ROOT / "tests/nuttx-std/bcmf-gspi-receive.c"), "-o", str(executable),
                ], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                subprocess.run([str(executable)], check=True, capture_output=True, text=True)

    def test_cyw43439_hci_transport(self):
        source, _, _, _ = self.fixture("nuttx")
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        driver = (source / "drivers/wireless/ieee80211/bcm43xxx/bcmf_gspi_bt.c").read_text()
        # Compile the actual complete implementation with mocked kernel/bus
        # boundaries, rather than duplicating its protocol in the test.
        (self.base / "bcmf-bt-transport.h").write_text(
            driver[driver.index("#define BCMF_BT_FW_BASE"):])
        netdev = (source / "drivers/wireless/ieee80211/bcm43xxx/bcmf_netdev.c").read_text()
        begin = netdev.index("static void bcmf_lowpower_ifdown_work(FAR void *arg)\n{")
        end = netdev.index("\n}\n", begin) + 3
        (self.base / "bcmf-bt-idle.h").write_text(netdev[begin:end])
        executable = self.base / "bcmf-bt"
        result = subprocess.run([
            "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread",
            "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
            "-I", str(self.base), str(ROOT / "tests/nuttx-std/bcmf-gspi-bt.c"),
            "-o", str(executable),
        ], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

        result = subprocess.run([str(executable)], capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bluetooth_hci_command_lifetime(self):
        source, _, _, _ = self.fixture("nuttx")
        core = source / "wireless/bluetooth/bt_hcicore.c"
        baseline = core.read_text()
        buffers = source / "wireless/bluetooth/bt_buf.c"
        baseline_buffers = buffers.read_text()
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        for label, text, flags in (("baseline", baseline, ["-DEXPECT_BASELINE_BUGS"]),
                                   ("patched", core.read_text(), [])):
            with self.subTest(hci=label):
                functions = []
                for name in ("static void hci_cmd_done(", "int bt_hci_cmd_send_sync("):
                    begin = text.index(name)
                    end = text.index("\n}\n", begin) + 3
                    functions.append(text[begin:end])
                (self.base / "bt-hci-command-functions.h").write_text("\n".join(functions))
                buffer_text = baseline_buffers if label == "baseline" else buffers.read_text()
                functions = []
                for name in ("void bt_buf_release(", "FAR struct bt_buf_s *bt_buf_addref("):
                    begin = buffer_text.index(name)
                    end = buffer_text.index("\n}\n", begin) + 3
                    functions.append(buffer_text[begin:end])
                (self.base / "bt-buffer-functions.h").write_text("\n".join(functions))
                executable = self.base / f"bt-hci-command-{label}"
                result = subprocess.run([
                    "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread",
                    "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                    *flags, "-I", str(self.base),
                    str(ROOT / "tests/nuttx-std/bt-hci-command.c"), "-o", str(executable),
                ], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = subprocess.run([str(executable)], capture_output=True,
                                        text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_bcmf_shared_backplane_window(self):
        source, _, _, _ = self.fixture("nuttx")
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        core = (source / "drivers/wireless/ieee80211/bcm43xxx/bcmf_core.c").read_text()
        functions = []
        for name in ("int bcmf_read_sbreg(", "int bcmf_write_sbreg("):
            begin = core.index(name)
            end = core.index("\n}\n", begin) + 3
            functions.append(core[begin:end])
        (self.base / "bcmf-backplane-functions.h").write_text("\n".join(functions))
        executable = self.base / "bcmf-backplane"
        compiled = subprocess.run([
            "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread",
            "-I", str(self.base), str(ROOT / "tests/nuttx-std/bcmf-backplane.c"),
            "-o", str(executable),
        ], capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        result = subprocess.run([str(executable)], capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cyw43439_irq_state(self):
        source, _, _, _ = self.fixture("nuttx")
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        text = (source / "arch/arm/src/rp23xx/rp23xx_cyw43439.c").read_text()
        reverse = subprocess.run(
            ["git", "apply", "--reverse",
             str(ROOT / "upstream/nuttx/patches/0015-rp23xx-cyw43439-irq-state.patch")],
            cwd=source, env={**os.environ,
                           "GIT_CEILING_DIRECTORIES": str(source.parent)},
            capture_output=True, text=True,
        )
        self.assertEqual(reverse.returncode, 0, reverse.stderr)
        original = (source / "arch/arm/src/rp23xx/rp23xx_cyw43439.c").read_text()

        def function(code, name):
            begin = code.index(name)
            end = code.index("\n}\n", begin) + 3
            return code[begin:end]

        for label, code, flags in (("baseline", original, ["-DEXPECT_BASELINE_BUG"]),
                                    ("patched", text, [])):
            functions = function(code, "static int my_interrupt_enable(")
            if label == "patched":
                functions += "\n" + function(code, "static void my_transfer_irq(")
            (self.base / "cyw43439-irq-functions.h").write_text(functions)
            executable = self.base / f"cyw-irq-{label}"
            compiled = subprocess.run([
                "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", *flags,
                "-I", str(self.base), str(ROOT / "tests/nuttx-std/cyw43439-irq.c"),
                "-o", str(executable),
            ], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_cdcacm_bootloader_trigger(self):
        source, _, _, _ = self.fixture("nuttx")
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        driver = (source / "drivers/usbdev/cdcacm.c").read_text()
        board = (source / "boards/arm/rp23xx/common/src/rp23xx_reset.c").read_text()

        def function(code, name):
            begin = code.index(name)
            return code[begin:code.index("\n}\n", begin) + 3]

        begin = driver.index("        case ACM_SET_LINE_CODING:")
        end = driver.index("        /* ACM_SET_CTRL_LINE_STATE:", begin)
        (self.base / "cdcacm-linecoding.h").write_text(driver[begin:end])
        (self.base / "cdcacm-reset.h").write_text(
            function(driver, "static void cdcacm_bootloader("))
        (self.base / "cdcacm-rom.h").write_text(
            function(board, "static void rp23xx_bootloader(") + "\n" +
            function(board, "void board_usbdev_bootloader("))
        for name in ("cdcacm_unbind(", "cdcuart_release("):
            # Driver unbind and final serial release cancel embedded timers.
            definition = driver.index(name, driver.index("static void cdcacm_unbind(") + 1)
            self.assertIn("wd_cancel(&priv->bootloader);",
                          driver[definition:driver.index("\n}\n", definition)])
        for enabled in (False, True):
            with self.subTest(reset_enabled=enabled):
                executable = self.base / f"cdcacm-reset-{enabled}"
                flags = ["-DCONFIG_CDCACM_1200BPS_BOOTLOADER"] if enabled else []
                result = subprocess.run([
                    "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", *flags,
                    "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                    "-I", str(self.base), str(ROOT / "tests/nuttx-std/cdcacm-bootloader.c"),
                    "-o", str(executable),
                ], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = subprocess.run([str(executable)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_rp23xx_usb_completion_rearm(self):
        source, upstream, _, _ = self.fixture("nuttx")
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        usb_path = "arch/arm/src/rp23xx/rp23xx_usbdev.c"
        fixture_usb = source / usb_path
        fixture_usb.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(upstream / usb_path, fixture_usb)
        driver = fixture_usb.read_text()

        def function(name):
            start = driver.rindex(name)
            end = driver.index("\n}\n", start) + 3
            return driver[start:end]

        definitions = "\n\n".join(function(signature) for signature in (
            "static void rp23xx_update_buffer_control(",
            "static void rp23xx_txcomplete(",
            "static void rp23xx_rxcomplete(",
        ))
        (self.base / "rp23xx-usb-functions.h").write_text(definitions)
        executable = self.base / "rp23xx-usb-rearm"
        compiled = subprocess.run([
            "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
            "-I", str(self.base),
            str(ROOT / "tests/nuttx-std/rp23xx-usb-rearm.c"),
            "-o", str(executable),
        ], capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        result = subprocess.run([str(executable)], capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
