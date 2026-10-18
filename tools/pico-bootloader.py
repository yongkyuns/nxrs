#!/usr/bin/env python3
"""Enter BOOTSEL through the opt-in USB reset hook, or safely reboot from ROM."""

import argparse
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def _nodes(roots):
    pending = list(roots)
    while pending:
        node = pending.pop()
        yield node
        pending.extend(node.get("IORegistryEntryChildren", ()))


def _bootloader_devices(registry, chip_serial):
    devices = {node["IORegistryEntryID"]: node for node in _nodes(registry)
               if node.get("IOObjectClass") == "IOUSBHostDevice"
               and node.get("idVendor") == 0x2e8a and node.get("idProduct") == 0x000f
               and node.get("USB Serial Number", "").upper() == chip_serial.upper()}
    if len(devices) > 1:
        raise RuntimeError("Requested RP2350 USB identity is ambiguous")
    return devices


def bootloader_disks(registry, chip_serial):
    """Resolve media beneath the exact USB device, never by volume label."""
    devices = _bootloader_devices(registry, chip_serial)
    if not devices:
        return None  # USB/media enumeration may still be in progress.
    nodes = list(_nodes(devices.values()))
    disks = {node["BSD Name"] for node in nodes if "BSD Name" in node}
    if not disks:
        # BOOTSEL can intentionally expose PICOBOOT without mass storage.
        interfaces = [node for node in nodes if node.get("IOObjectClass") == "IOUSBHostInterface"]
        if (not interfaces or any("bInterfaceClass" not in node for node in interfaces)
                or any(node["bInterfaceClass"] == 8 for node in interfaces)):
            return None
        return () if any(node["bInterfaceClass"] == 0xff for node in interfaces) else None
    whole = [name for name in disks if re.fullmatch(r"disk[0-9]+", name)]
    if len(whole) != 1 or any(not re.fullmatch(re.escape(whole[0]) + r"(?:s[0-9]+)*", name)
                              for name in disks):
        raise RuntimeError("Requested RP2350 disk identity is ambiguous")
    return (whole[0], *sorted(disks - set(whole)))


def _run(argv, timeout=5):
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("RP2350 USB maintenance command failed or timed out") from error


def _plist(argv):
    result = _run(argv)
    if result.returncode:
        raise RuntimeError("Could not inspect requested RP2350 disk safely")
    try:
        return plistlib.loads(result.stdout)
    except (ValueError, plistlib.InvalidFileException) as error:
        raise RuntimeError("Could not inspect requested RP2350 disk safely") from error


def _bootloader_registry(chip_serial):
    # Expanding every USB driver's properties can stall on unrelated devices.
    # Identify the chip in a shallow snapshot, then expand only its subtree.
    command = ["/usr/sbin/ioreg", "-a", "-l", "-p", "IOService", "-r", "-c", "IOUSBHostDevice"]
    devices = _bootloader_devices(_plist(command + ["-d", "1"]), chip_serial)
    if not devices:
        return []
    device = next(iter(devices.values()))
    name = device.get("IORegistryEntryName")
    location = device.get("IORegistryEntryLocation")
    if not isinstance(name, str) or not name:
        raise RuntimeError("Requested RP2350 USB registry name is unavailable")
    if location:
        name += "@" + location
    registry = _plist(command + ["-n", name])
    if _bootloader_devices(registry, chip_serial).keys() != devices.keys():
        raise RuntimeError("Requested RP2350 USB identity changed during inspection")
    return registry


def stock_boot_disk(disks, metadata):
    """Only the ROM's synthetic FAT disk is eligible for forced unmount."""
    return (len(disks) == 2 and disks[1] == disks[0] + "s1"
            and metadata[0].get("MediaName") == "RP2350"
            and metadata[0].get("RemovableMedia") is True
            and metadata[0].get("Content") == "FDisk_partition_scheme"
            and metadata[0].get("Size") == 128 * 1024 * 1024
            and metadata[1].get("Content") == "Windows_FAT_16"
            and metadata[1].get("FilesystemType") == "msdos"
            and metadata[1].get("VolumeName") == "RP2350"
            and metadata[1].get("Size") == 128 * 1024 * 1024 - 512)


def unmount_bootloader(chip_serial, timeout=10, *, allow_force=False):
    """Keep PICOBOOT available, but detach its macOS filesystem before reset."""
    if sys.platform != "darwin":
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        disks = bootloader_disks(_bootloader_registry(chip_serial), chip_serial)
        if disks is not None:
            break
        time.sleep(0.2)
    else:
        raise RuntimeError("Requested RP2350 disk did not finish enumerating; refusing reboot")
    if not disks:
        return

    def info(name):
        value = _plist(["/usr/sbin/diskutil", "info", "-plist", "/dev/" + name])
        if (value.get("DeviceIdentifier") != name or value.get("Internal") is not False
                or value.get("BusProtocol") != "USB"
                or value.get("WholeDisk") is not (name == disks[0])):
            raise RuntimeError("Requested RP2350 disk metadata changed; refusing reboot")
        return value

    for name in disks:
        info(name)
    try:
        result = _run(["/usr/sbin/diskutil", "unmountDisk", "/dev/" + disks[0]], timeout=10)
    except RuntimeError:
        if not allow_force:
            raise
        result = None
    if allow_force and (result is None or result.returncode):
        # Resolve again: a removed/replaced disk must never inherit approval.
        if bootloader_disks(_bootloader_registry(chip_serial), chip_serial) != disks:
            raise RuntimeError("Requested RP2350 disk changed; refusing forced unmount")
        if not stock_boot_disk(disks, [info(name) for name in disks]):
            raise RuntimeError("Disk is not the stock RP2350 virtual volume; refusing forced unmount")
        result = _run(["/usr/sbin/diskutil", "unmountDisk", "force", "/dev/" + disks[0]], timeout=10)
    if (result.returncode or bootloader_disks(_bootloader_registry(chip_serial), chip_serial) != disks
            or any(info(name).get("MountPoint") for name in disks)):
        raise RuntimeError("Requested RP2350 disk could not be unmounted; refusing reboot")


def reboot_bootloader(picotool, chip_serial, *, allow_force=False):
    if not in_bootloader(picotool, chip_serial):
        raise RuntimeError("Requested RP2350 is not in BOOTSEL; refusing reboot")
    unmount_bootloader(chip_serial, allow_force=allow_force)
    result = _run([str(picotool), "reboot", "--ser", chip_serial], timeout=10)
    if result.returncode:
        raise RuntimeError("Requested RP2350 did not reboot")


def in_bootloader(picotool, chip_serial):
    try:
        result = subprocess.run(
            [str(picotool), "info", "--ser", chip_serial],
            capture_output=True, text=True, timeout=2,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0 and "RP2350" in result.stdout


def enter_bootloader(port, picotool, chip_serial, timeout=10):
    # Check the exact ROM identity first; don't touch another serial device if
    # the requested board is already in BOOTSEL. Never invoke picotool -f.
    if in_bootloader(picotool, chip_serial):
        return
    import serial

    touch_error = None
    try:
        with serial.Serial(port, baudrate=1200, timeout=0.2, write_timeout=0.2) as device:
            device.dtr = False
    except serial.SerialException as error:
        # A successful reset can make the port disappear during open/close.
        # Only the exact ROM enumeration below can turn this into success.
        touch_error = error
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if in_bootloader(picotool, chip_serial):
            return
        time.sleep(0.2)
    raise RuntimeError("Requested Pico did not enter BOOTSEL; USB/timer handling "
                       "may be stuck or the reset hook may not be installed") from touch_error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="Explicit Pico USB serial port for 1200-baud entry")
    parser.add_argument("--reboot", action="store_true", help="Safely leave BOOTSEL instead of entering it")
    parser.add_argument("--force-boot-disk", action="store_true",
                        help="Allow forced unmount of the verified virtual ROM disk if normal unmount fails")
    parser.add_argument("--serial", required=True, help="16-digit chip serial from picotool info -a")
    parser.add_argument("--picotool", type=Path,
                        default=shutil.which("picotool") or
                        ROOT / "target/radio-deps/picotool-build/picotool")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-fA-F]{16}", args.serial):
        parser.error("--serial must be a 16-digit hexadecimal Pico chip serial")
    if not args.picotool.is_file():
        parser.error("picotool not found; provide --picotool PATH")
    if not args.reboot and not args.port:
        parser.error("--port is required to enter BOOTSEL")
    if args.force_boot_disk and not args.reboot:
        parser.error("--force-boot-disk requires --reboot")
    try:
        if args.reboot:
            reboot_bootloader(args.picotool, args.serial.upper(), allow_force=args.force_boot_disk)
        else:
            enter_bootloader(args.port, args.picotool, args.serial.upper())
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"{error}\n")
    print("Requested RP2350 rebooted" if args.reboot else "BOOTSEL confirmed for the requested RP2350")


if __name__ == "__main__":
    main()
