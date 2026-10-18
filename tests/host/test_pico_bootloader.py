"""Host reset requests must be confirmed against the explicitly chosen chip."""
import importlib.util
from pathlib import Path
import subprocess
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("pico_bootloader", ROOT / "tools/pico-bootloader.py")
RESET = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RESET)


class PicoBootloaderTests(unittest.TestCase):
    def setUp(self):
        unmount = patch.object(RESET, "unmount_bootloader")
        self.unmount = unmount.start()
        self.addCleanup(unmount.stop)

    def test_already_in_bootloader_does_not_open_serial(self):
        with patch.object(RESET, "in_bootloader", return_value=True), \
             patch.dict("sys.modules", {"serial": None}):
            RESET.enter_bootloader("explicit-port", "picotool", "0123456789ABCDEF")
        # Entering ROM must not prevent a critical flash restore just because
        # Spotlight is busy. Only withdrawing the USB device needs unmount.
        self.unmount.assert_not_called()

    def test_touch_uses_1200_baud_and_confirms_identity(self):
        serial = types.SimpleNamespace(Serial=MagicMock(), SerialException=OSError)
        with patch.dict("sys.modules", {"serial": serial}), \
             patch.object(RESET, "in_bootloader", side_effect=[False, True]) as check:
            RESET.enter_bootloader("explicit-port", "picotool", "0123456789ABCDEF")
        serial.Serial.assert_called_once_with("explicit-port", baudrate=1200,
                                             timeout=0.2, write_timeout=0.2)
        self.assertFalse(serial.Serial.return_value.__enter__.return_value.dtr)
        self.assertEqual(check.call_args_list[-1].args, ("picotool", "0123456789ABCDEF"))

    def test_port_disappearance_is_success_only_after_rom_confirmation(self):
        serial = types.SimpleNamespace(Serial=Mock(side_effect=OSError("disconnected")),
                                       SerialException=OSError)
        with patch.dict("sys.modules", {"serial": serial}), \
             patch.object(RESET, "in_bootloader", side_effect=[False, True]):
            RESET.enter_bootloader("explicit-port", "picotool", "0123456789ABCDEF")
        with patch.dict("sys.modules", {"serial": serial}), \
             patch.object(RESET, "in_bootloader", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "did not enter BOOTSEL"):
                RESET.enter_bootloader("explicit-port", "picotool", "0123456789ABCDEF", timeout=0)

    def test_picotool_queries_exact_chip_without_force_reset(self):
        result = subprocess.CompletedProcess([], 0, stdout="target chip: RP2350")
        with patch.object(RESET.subprocess, "run", return_value=result) as run:
            self.assertTrue(RESET.in_bootloader("picotool", "0123456789ABCDEF"))
        self.assertEqual(run.call_args.args[0],
                         ["picotool", "info", "--ser", "0123456789ABCDEF"])
        for result in (subprocess.CompletedProcess([], 1, stdout="RP2350"),
                       subprocess.CompletedProcess([], 0, stdout="target chip: RP2040")):
            with patch.object(RESET.subprocess, "run", return_value=result):
                self.assertFalse(RESET.in_bootloader("picotool", "0123456789ABCDEF"))


class MacUnmountTests(unittest.TestCase):
    serial = "0123456789ABCDEF"

    def registry(self, *, serial=None, storage=True, media=True):
        return [{"IOObjectClass": "IOUSBHostDevice", "IORegistryEntryID": 10,
                 "IORegistryEntryName": "RP2350 Boot", "IORegistryEntryLocation": "12300000",
                 "idVendor": 0x2e8a, "idProduct": 0x000f,
                 "USB Serial Number": self.serial if serial is None else serial,
                 "IORegistryEntryChildren": [
                     {"IOObjectClass": "IOUSBHostInterface", "bInterfaceClass": 8 if storage else 0xff,
                      "IORegistryEntryChildren": ([{"BSD Name": "disk7"},
                                                   {"BSD Name": "disk7s1"}] if media else [])}]}]

    def test_disk_selection_uses_chip_serial_not_volume_name(self):
        registry = self.registry(serial="FEDCBA9876543210")
        registry[0]["IORegistryEntryName"] = "RP2350"
        self.assertIsNone(RESET.bootloader_disks(registry, self.serial))
        self.assertEqual(RESET.bootloader_disks(self.registry(), self.serial), ("disk7", "disk7s1"))
        self.assertEqual(RESET.bootloader_disks(self.registry() * 2, self.serial), ("disk7", "disk7s1"))

    def test_pending_media_differs_from_picoboot_without_storage(self):
        self.assertIsNone(RESET.bootloader_disks(self.registry(media=False), self.serial))
        self.assertEqual(RESET.bootloader_disks(self.registry(storage=False, media=False), self.serial), ())
        pending = self.registry(storage=False, media=False)
        del pending[0]["IORegistryEntryChildren"][0]["bInterfaceClass"]
        self.assertIsNone(RESET.bootloader_disks(pending, self.serial))
        pending[0]["IORegistryEntryChildren"] = []
        self.assertIsNone(RESET.bootloader_disks(pending, self.serial))

    def test_registry_expands_only_the_exact_chip_after_shallow_selection(self):
        for located in (True, False):
            with self.subTest(located=located):
                registry = self.registry()
                if not located:
                    del registry[0]["IORegistryEntryLocation"]
                unrelated = self.registry(serial="FEDCBA9876543210")
                unrelated[0]["IORegistryEntryID"] = 20
                unrelated[0]["IORegistryEntryName"] = "Unrelated USB device"
                with patch.object(RESET, "_plist", side_effect=[unrelated + registry, registry]) as inspect:
                    self.assertEqual(RESET._bootloader_registry(self.serial), registry)
                commands = [call.args[0] for call in inspect.call_args_list]
                self.assertEqual(commands[0][-2:], ["-d", "1"])
                self.assertEqual(commands[1][-2:], ["-n", "RP2350 Boot" +
                                                   ("@12300000" if located else "")])
                self.assertEqual(commands[0][:-2], commands[1][:-2])

    def test_registry_missing_chip_never_expands_any_subtree(self):
        with patch.object(RESET, "_plist", return_value=self.registry(serial="other")) as inspect:
            self.assertEqual(RESET._bootloader_registry(self.serial), [])
        inspect.assert_called_once()
        self.assertEqual(inspect.call_args.args[0][-2:], ["-d", "1"])

    def test_registry_rechecks_identity_after_name_lookup(self):
        for replacement in ([], self.registry(serial="other"), self.registry()):
            if replacement and replacement[0]["USB Serial Number"] == self.serial:
                replacement[0]["IORegistryEntryID"] = 11
            with self.subTest(replacement_present=bool(replacement)), \
                 patch.object(RESET, "_plist", side_effect=[self.registry(), replacement]):
                with self.assertRaisesRegex(RuntimeError, "identity changed"):
                    RESET._bootloader_registry(self.serial)
        registry = self.registry()
        del registry[0]["IORegistryEntryName"]
        with patch.object(RESET, "_plist", return_value=registry) as inspect:
            with self.assertRaisesRegex(RuntimeError, "name is unavailable"):
                RESET._bootloader_registry(self.serial)
        inspect.assert_called_once()

    def test_duplicate_chips_or_unrelated_media_are_rejected(self):
        other = self.registry()[0]
        other["IORegistryEntryID"] = 11
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            RESET.bootloader_disks(self.registry() + [other], self.serial)
        for name in ("disk8s1", "disk8", "../../disk7"):
            registry = self.registry()
            registry[0]["IORegistryEntryChildren"].append({"BSD Name": name})
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                RESET.bootloader_disks(registry, self.serial)

    def exercise(self, *, busy=False, internal=False, remount=False, changed=False, allow_force=False):
        unmounted = False
        def inspect(argv):
            if argv[0] == "/usr/sbin/ioreg":
                registry = self.registry()
                if unmounted and changed:
                    registry[0]["IORegistryEntryChildren"].append({"BSD Name": "disk7s2"})
                return registry
            name = argv[-1].removeprefix("/dev/")
            return {"DeviceIdentifier": name, "Internal": internal, "BusProtocol": "USB",
                    "WholeDisk": name == "disk7",
                    "MountPoint": "/Volumes/RP2350" if name.endswith("s1") and
                        (not unmounted or remount) else ""}
        def run(argv, **kwargs):
            nonlocal unmounted
            self.assertEqual(argv, ["/usr/sbin/diskutil", "unmountDisk", "/dev/disk7"])
            unmounted = True
            return subprocess.CompletedProcess(argv, int(busy))
        with patch.object(RESET.sys, "platform", "darwin"), \
             patch.object(RESET, "_plist", side_effect=inspect), \
             patch.object(RESET.subprocess, "run", side_effect=run) as command:
            if busy or internal or remount or changed:
                with self.assertRaises(RuntimeError):
                    RESET.unmount_bootloader(self.serial, allow_force=allow_force)
            else:
                RESET.unmount_bootloader(self.serial, allow_force=allow_force)
        return command

    def test_unmount_is_nonforced_and_only_targets_the_matching_disk(self):
        self.exercise().assert_called_once()

    def test_busy_internal_or_remounted_disk_prevents_success(self):
        self.exercise(busy=True).assert_called_once()
        self.exercise(remount=True).assert_called_once()
        self.exercise(changed=True).assert_called_once()
        self.exercise(internal=True).assert_not_called()

    def test_other_hosts_need_no_macos_tools(self):
        with patch.object(RESET.sys, "platform", "linux"), patch.object(RESET, "_plist") as inspect:
            RESET.unmount_bootloader(self.serial)
        inspect.assert_not_called()

    def test_reboot_waits_for_successful_unmount_and_never_forces(self):
        with patch.object(RESET, "in_bootloader", return_value=True), \
             patch.object(RESET, "unmount_bootloader", side_effect=RuntimeError("busy")), \
             patch.object(RESET.subprocess, "run") as command:
            with self.assertRaisesRegex(RuntimeError, "busy"):
                RESET.reboot_bootloader("picotool", self.serial)
        command.assert_not_called()
        actions = []
        with patch.object(RESET, "in_bootloader", return_value=True), \
             patch.object(RESET, "unmount_bootloader", side_effect=lambda _, **kw: actions.append("unmount")), \
             patch.object(RESET.subprocess, "run", side_effect=lambda argv, **kw:
                          (actions.append("reboot") or subprocess.CompletedProcess(argv, 0))) as command:
            RESET.reboot_bootloader("picotool", self.serial)
        self.assertEqual(actions, ["unmount", "reboot"])
        self.assertEqual(command.call_args.args[0], ["picotool", "reboot", "--ser", self.serial])

    def test_wrong_rom_never_unmounts_or_reboots(self):
        with patch.object(RESET, "in_bootloader", return_value=False), \
             patch.object(RESET, "unmount_bootloader") as unmount, \
             patch.object(RESET.subprocess, "run") as command:
            with self.assertRaisesRegex(RuntimeError, "not in BOOTSEL"):
                RESET.reboot_bootloader("picotool", self.serial)
        unmount.assert_not_called()
        command.assert_not_called()

    def test_command_timeouts_never_print_backend_arguments(self):
        with patch.object(RESET.subprocess, "run", side_effect=subprocess.TimeoutExpired(
                ["tool", "private-identifier"], 10)):
            with self.assertRaisesRegex(RuntimeError, "failed or timed out") as error:
                RESET._run(["tool", "private-identifier"])
        self.assertNotIn("private-identifier", str(error.exception))

    def test_force_fallback_is_opt_in_and_limited_to_verified_stock_media(self):
        for wrong_size, remount, changed, timeout in (
                (False, False, False, False), (True, False, False, False),
                (False, True, False, False), (False, False, True, False),
                (False, False, False, True)):
            with self.subTest(wrong_size=wrong_size, remount=remount, changed=changed, timeout=timeout):
                commands = []
                def inspect(argv):
                    if argv[0] == "/usr/sbin/ioreg":
                        registry = self.registry()
                        if changed and commands:
                            registry[0]["IORegistryEntryChildren"].append({"BSD Name": "disk7s2"})
                        return registry
                    name = argv[-1].removeprefix("/dev/")
                    whole = name == "disk7"
                    return {"DeviceIdentifier": name, "Internal": False, "BusProtocol": "USB",
                            "WholeDisk": whole, "RemovableMedia": True,
                            "Content": "FDisk_partition_scheme" if whole else "Windows_FAT_16",
                            "MediaName": "RP2350" if whole else "", "VolumeName": "" if whole else "RP2350",
                            "FilesystemType": "msdos", "Size": 1 if wrong_size else 134217728 - (0 if whole else 512),
                            "MountPoint": "/Volumes/RP2350" if not whole and
                                (len(commands) < 2 or remount) else ""}
                def run(argv, **kwargs):
                    commands.append(argv)
                    self.assertEqual(argv[-1], "/dev/disk7")
                    if timeout and "force" not in argv:
                        raise subprocess.TimeoutExpired(argv, 10)
                    return subprocess.CompletedProcess(argv, int("force" not in argv))
                with patch.object(RESET.sys, "platform", "darwin"), \
                     patch.object(RESET, "_plist", side_effect=inspect), \
                     patch.object(RESET.subprocess, "run", side_effect=run):
                    if wrong_size or remount or changed:
                        with self.assertRaises(RuntimeError):
                            RESET.unmount_bootloader(self.serial, allow_force=True)
                    else:
                        RESET.unmount_bootloader(self.serial, allow_force=True)
                self.assertEqual(len(commands), 1 if wrong_size or changed else 2)
                if len(commands) == 2:
                    self.assertEqual(commands[1], ["/usr/sbin/diskutil", "unmountDisk", "force", "/dev/disk7"])

    def test_normal_success_never_needs_force_even_when_allowed(self):
        self.exercise(allow_force=True).assert_called_once()

    def test_reboot_passes_explicit_permission_to_unmount(self):
        with patch.object(RESET, "unmount_bootloader") as unmount, \
             patch.object(RESET, "in_bootloader", return_value=True), \
             patch.object(RESET.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            RESET.reboot_bootloader("picotool", self.serial, allow_force=True)
        unmount.assert_called_once_with(self.serial, allow_force=True)


if __name__ == "__main__":
    unittest.main()
