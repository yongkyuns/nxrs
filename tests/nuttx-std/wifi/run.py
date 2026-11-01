#!/usr/bin/env python3
"""Local station qualification. Firmware must already be flashed and at NSH."""
import argparse
import ipaddress
import os
from pathlib import Path
import re
import socket
import stat
import threading
import time

ROOT = Path(__file__).resolve().parents[3]


def password_bytes(path):
    path = Path(path).absolute()
    if path.resolve().is_relative_to(ROOT):
        raise ValueError("keep the password file outside the repository")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077):
            raise ValueError("password file must be owned by you, regular, and mode 600 or stricter")
        value = os.read(fd, 65)
    finally:
        os.close(fd)
    if value.endswith(b"\n"):
        value = value[:-1]
    if not 8 <= len(value) <= 63 or any(byte < 32 or byte > 126 for byte in value):
        raise ValueError("expected one WPA2 passphrase of 8–63 printable ASCII bytes")
    return value


def expected_packet(sequence):
    size = (16, 64, 256, 512)[sequence % 4]
    return sequence.to_bytes(4, "big") + bytes((offset + sequence) % 256 for offset in range(size - 4))


def read_until(console, pending, marker, password, timeout, *, read_size=512):
    markers = (marker,) if isinstance(marker, bytes) else marker
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Never print or save raw console data, including failures.
        if password and password in pending:
            raise RuntimeError("credential echo detected; console output withheld")
        if b"NXRS_WIFI_STATION_FAIL" in pending:
            station = re.search(
                rb"NXRS_WIFI_STATION_FAIL error=(Busy|Unavailable|Unsupported|PermissionDenied|InvalidConfig|AuthenticationFailed|Timeout|Io|Protocol)(?:[\r\n]|$)",
                pending,
            )
            detail = re.search(
                rb"NXRS_WIFI_TRAFFIC_FAIL phase=(send|receive) sequence=(\d+) kind=([A-Za-z]+)",
                pending,
            )
            suffix = (f"; {station[1].decode()}" if station else "")
            suffix += (f"; traffic {detail[1].decode()} sequence {int(detail[2])}: "
                      f"{detail[3].decode()}" if detail else "")
            arp = re.search(rb"arp_wait failed: (-?\d+),", pending)
            if arp:
                suffix += f"; ARP wait error {int(arp[1])}"
            raise RuntimeError(f"device reported station failure; no pass{suffix}")
        found = next((value for value in markers if value in pending), None)
        if found is not None:
            marker = found
            end = pending.index(marker) + len(marker)
            result = bytes(pending[:end])
            del pending[:end]
            return result
        pending.extend(console.read(read_size))
        if len(pending) > 8192:
            del pending[:-4096]
    raise RuntimeError("timed out waiting for the device test stage")


def qualify(port, host_ip, ssid, password):
    import serial  # Use the same pyserial environment as esptool.

    served = set()
    received = 0
    invalid = 0
    stopped = threading.Event()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as echo:
        echo.bind((host_ip, 0))
        echo.settimeout(0.2)

        def serve():
            nonlocal received, invalid
            while not stopped.is_set():
                try:
                    data, peer = echo.recvfrom(513)
                except socket.timeout:
                    continue
                received += 1
                if len(data) < 4:
                    invalid += 1
                    continue
                sequence = int.from_bytes(data[:4], "big")
                if sequence < 20 and data == expected_packet(sequence):
                    echo.sendto(data, peer)
                    served.add(sequence)
                else:
                    invalid += 1

        worker = threading.Thread(target=serve, name="wifi-test-echo")
        worker.start()
        try:
            # Avoid implicit board reset on opening the USB serial console.
            console = serial.Serial()
            console.port, console.baudrate, console.timeout = port, 115200, 0.1
            console.dtr = console.rts = False
            console.open()
            with console:
                pending = bytearray()

                def until(marker, timeout):
                    return read_until(console, pending, marker, password, timeout)

                # NuttX's USB NSH waits for three consecutive line breaks.
                # Ordinary UART/USB-JTAG shells also accept these empty lines.
                console.write(b"\n\n\n")
                until(b"nsh> ", 5)
                peer = f"{host_ip}:{echo.getsockname()[1]}"
                console.write(f"wifi_test station {peer}\n".encode("ascii"))
                until(b"NXRS_WIFI_CREDENTIALS_READY", 5)
                console.write(bytes((len(ssid), len(password))) + ssid + password)
                until(b"NXRS_WIFI_ASSOCIATED_DHCP", 35)
                print("Association and DHCP confirmed.")
                tail = until(b"NXRS_WIFI_STATION_PASS", 60)
                if b"nsh> " not in tail:
                    until(b"nsh> ", 5)
                console.write(b"echo $?\n")
                status = until(b"nsh> ", 5)
                if not re.search(rb"(?:^|[\r\n])0[\r\n]", status):
                    raise RuntimeError("NSH exit status was not zero")
        finally:
            stopped.set()
            worker.join(timeout=2)
            print(f"Host UDP evidence: received={received}, valid_sequences={len(served)}, invalid={invalid}.")
    if served != set(range(20)):
        raise RuntimeError("Mac did not receive every expected test packet")
    print("PASS: 20 exact UDP round trips, disconnect confirmed, provider joined, NSH status zero.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--host-ip", required=True, help="Mac IPv4 address on the test Wi-Fi/LAN")
    parser.add_argument("--ssid", required=True)
    secret = parser.add_mutually_exclusive_group(required=True)
    secret.add_argument("--password-file", type=Path)
    secret.add_argument("--open-network", action="store_true")
    args = parser.parse_args()
    host = ipaddress.IPv4Address(args.host_ip)
    if host.is_unspecified or host.is_multicast or host.is_loopback:
        parser.error("host-ip must be the Mac's LAN address")
    ssid = args.ssid.encode("utf-8")
    if not 1 <= len(ssid) <= 32:
        parser.error("SSID must encode to 1–32 bytes")
    try:
        password = password_bytes(args.password_file) if args.password_file else b""
        qualify(args.port, str(host), ssid, password)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"FAIL: {error}\n")


if __name__ == "__main__":
    main()
