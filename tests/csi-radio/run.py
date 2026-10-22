#!/usr/bin/env python3
"""Explicit spare-board run. Requires an already-flashed idle NSH; never flashes/resets."""
import argparse
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import threading
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SPEC = importlib.util.spec_from_file_location(
    "wifi_credentials", ROOT / "tests/nuttx-std/wifi/run.py"
)
WIFI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WIFI)
TAG = b"NXRS-CSIRAD!"
RAW = re.compile(
    rb"CSI_RAW ts_us=\d+ channel=\d+ secondary=\d+ len=\d+ invalid_first=[01] rssi=-?\d+ agc=\d+ fft=-?\d+ rx=[0-9a-f]{48,128} csi=[0-9a-f]{2,1224}"
)
RESULT = re.compile(
    rb"CSI_CAPTURE_RESULT frames=(\d+) factors=(\d+) echoes=(\d+) raw_exported=(\d+) force_exercised=([01]) loss=0 advisory_only=1"
)


def validate_report(data, served, force):
    if (
        b"CSI_RADIO_FAIL" in data
        or b"CSI_CLEANUP_FAILED" in data
        or b"CSI_CHECK_FAIL" in data
    ):
        raise ValueError("device reported failure; raw console withheld")
    reports = RESULT.findall(data)
    if len(reports) != 1:
        raise ValueError("missing or duplicate capture report")
    frames, factors, echoes, exported, forced = map(int, reports[0])
    if (
        frames < 50
        or factors == 0
        or echoes < 10
        or bool(forced) != force
        or echoes > 3001
        or echoes != len(served)
        or served != set(range(echoes))
    ):
        raise ValueError("incomplete RF/echo evidence or wrong gain mode")
    if (
        len(
            re.findall(
                rb"(?:^|[\r\n])CSI_RADIO_PASS cleanup=confirmed station=disconnected worker=joined advisory_only=1(?:[\r\n]|$)",
                data,
            )
        )
        != 1
    ):
        raise ValueError("confirmed CSI/station shutdown missing")
    return dict(
        frames=frames,
        factors=factors,
        echoes=echoes,
        raw_exported=exported,
        force_exercised=bool(forced),
        scope="capture and transport API trial only",
        gain_metadata_abi="unverified",
        presence_accuracy="not_tested",
        camera_coexistence="not_tested",
    )


def private_capture(path):
    path = Path(path).absolute()
    if path.resolve().is_relative_to(ROOT):
        raise ValueError("save RF captures outside the repository")
    return os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb"
    )


def qualify(args, ssid, password):
    import serial

    served = set()
    invalid = 0
    stopped = threading.Event()
    evidence = bytearray()
    pending = bytearray()
    raw_records = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as echo:
        echo.bind((args.host_ip, 0))
        echo.settimeout(0.2)

        def serve():
            nonlocal invalid
            while not stopped.is_set():
                try:
                    data, address = echo.recvfrom(17)
                except socket.timeout:
                    continue
                sequence = int.from_bytes(data[:4], "big")
                if (
                    len(data) != 16
                    or data[4:] != TAG
                    or sequence >= args.seconds * 10 + 1
                    or sequence in served
                ):
                    invalid += 1
                    continue
                echo.sendto(data, address)
                served.add(sequence)

        capture = private_capture(args.capture) if args.capture else None
        worker = threading.Thread(target=serve, name="csi-radio-echo")
        worker.start()
        try:
            console = serial.Serial()
            console.port = args.port
            console.baudrate = 115200
            console.timeout = 0.1
            console.dtr = False
            console.rts = False
            console.exclusive = True
            console.open()
            with console:

                def collect_lines(value):
                    nonlocal raw_records
                    for line in value.splitlines():
                        if line.startswith(
                            (
                                b"CSI_CAPTURE_RESULT ",
                                b"CSI_RADIO_PASS ",
                                b"CSI_RADIO_FAIL",
                                b"CSI_CLEANUP_FAILED",
                            )
                        ):
                            if len(evidence) + len(line) > 8192:
                                raise RuntimeError("trial reports exceeded bound")
                            evidence.extend(line + b"\n")
                        if capture and RAW.fullmatch(line.strip()):
                            fields = dict(
                                part.split(b"=", 1) for part in line.split()[1:]
                            )
                            length = int(fields[b"len"])
                            if (
                                len(fields[b"csi"]) != 2 * length
                                or length > 612
                                or length % 2
                                or not 1 <= int(fields[b"channel"]) <= 14
                            ):
                                raise RuntimeError("malformed RF capture record")
                            capture.write(line.strip() + b"\n")
                            raw_records += 1

                def until(marker, timeout, collect=False):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        if password and password in pending:
                            raise RuntimeError(
                                "credential echo detected; console withheld"
                            )
                        if (
                            b"CSI_RADIO_FAIL" in pending
                            or b"CSI_CLEANUP_FAILED" in pending
                        ):
                            raise RuntimeError("CSI trial failed; console withheld")
                        if marker in pending:
                            end = pending.index(marker) + len(marker)
                            value = bytes(pending[:end])
                            del pending[:end]
                            if collect:
                                collect_lines(value)
                            return value
                        if collect:
                            # Drain complete lines; save only the strict RF-record format,
                            # never arbitrary SDK/console/credential text.
                            while b"\n" in pending:
                                end = pending.index(b"\n") + 1
                                line = bytes(pending[:end])
                                del pending[:end]
                                collect_lines(line)
                        pending.extend(console.read(1024))
                        if len(pending) > 8192:
                            raise RuntimeError("console line exceeded bound")
                    raise RuntimeError("device stage deadline exceeded")

                console.write(b"\n\n\n")
                until(b"nsh> ", 5)
                host = f"{args.host_ip}:{echo.getsockname()[1]}"
                options = (" --raw" if args.capture else "") + (
                    " --force-baseline" if args.force_baseline else ""
                )
                console.write(
                    f"csi_radio --spare-board {args.seconds} {args.bssid} {host}{options}\n".encode(
                        "ascii"
                    )
                )
                until(b"NXRS_WIFI_CREDENTIALS_READY", 5)
                console.write(bytes((len(ssid), len(password))) + ssid + password)
                until(b"NXRS_WIFI_ASSOCIATED_DHCP", 35)
                until(b"nsh> ", args.seconds + 15, True)
                console.write(b"echo CSI_STATUS_$?\n")
                status = until(b"nsh> ", 5)
                if re.findall(rb"(?:^|[\r\n])CSI_STATUS_(\d+)[\r\n]", status) != [b"0"]:
                    raise RuntimeError("nonzero or missing NSH status")
        finally:
            if capture:
                capture.close()
            stopped.set()
            worker.join(timeout=2)
    if worker.is_alive() or invalid:
        raise RuntimeError("invalid UDP traffic or echo worker did not stop")
    report = validate_report(bytes(evidence), served, args.force_baseline)
    if not args.capture and report["raw_exported"] != 0:
        raise RuntimeError("unexpected raw export mode")
    if args.capture and not 1 <= report["raw_exported"] <= 3001:
        raise RuntimeError("raw export count outside bounded trial")
    if args.capture and report["raw_exported"] != raw_records:
        raise RuntimeError("RF export record count mismatch")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ack-spare-board", required=True, action="store_true")
    parser.add_argument(
        "--port",
        required=True,
        help="explicit spare-board port; no automatic discovery",
    )
    parser.add_argument("--host-ip", required=True)
    parser.add_argument("--bssid", required=True)
    parser.add_argument("--ssid", required=True)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument(
        "--capture",
        type=Path,
        help="new private file outside repository; sampled raw CSI",
    )
    parser.add_argument(
        "--force-baseline",
        action="store_true",
        help="separate RF-changing trial on the spare board only",
    )
    secret = parser.add_mutually_exclusive_group(required=True)
    secret.add_argument("--password-file", type=Path)
    secret.add_argument("--open-network", action="store_true")
    args = parser.parse_args()
    try:
        ip = ipaddress.IPv4Address(args.host_ip)
        if ip.is_unspecified or ip.is_multicast or ip.is_loopback:
            raise ValueError("host-ip must be a test LAN IPv4 address")
        if not 10 <= args.seconds <= 300:
            raise ValueError("seconds must be 10..300")
        if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", args.bssid):
            raise ValueError("BSSID must contain six hex octets")
        mac = bytes.fromhex(args.bssid.replace(":", ""))
        if mac == bytes(6) or mac[0] & 1:
            raise ValueError("BSSID must be unicast")
        ssid = args.ssid.encode("utf8")
        if not 1 <= len(ssid) <= 32:
            raise ValueError("SSID must be 1..32 bytes")
        password = (
            WIFI.password_bytes(args.password_file) if args.password_file else b""
        )
        report = qualify(args, ssid, password)
        print(json.dumps(report, sort_keys=True))
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"FAIL: {error}\n")


if __name__ == "__main__":
    main()
