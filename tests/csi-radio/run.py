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
TRANSPORT_RESULT = re.compile(rb"CSI_TRANSPORT_RESULT echoes=(\d+) csi_configured=0")
OPTIONS = re.compile(
    rb"CSI_TEST_OPTIONS transport_only=([01]) raw=([01]) force=([01]) peer_port=(\d{1,5}) tcp=([01])"
)
DIAGNOSTIC = re.compile(
    rb"(?:CSI_SDK_ERROR status=-?\d{1,10}|CSI_START_FAILED error=SdkError\(-?\d{1,10}\)|"
    rb"CSI_TEST_OPTIONS transport_only=[01] raw=[01] force=[01] peer_port=\d{1,5} tcp=[01]|"
    rb"CSI_TRAFFIC protocol=(?:udp|tcp)|"
    rb"CSI_TCP_CLOSED peer_eof=1|"
    rb"CSI_IO_ERROR stage=(?:tcp_connect|tcp_nodelay|tcp_nonblocking|udp_bind|udp_connect|udp_nonblocking) os_error=-?\d{1,10}|"
    rb"NXRS_WIFI_STAGE_FAIL stage=(?:ready|submit|connect|traffic|disconnect) error=(?:InvalidConfig|Busy|Unavailable|Timeout|Io|Protocol)|"
    rb"NXRS_WIFI_OBSERVATION stage=(?:ready|submit|connect|traffic|disconnect|cleanup) status=(?:Starting|Ready|Stopping|Stopped|Failed) link=(?:Unknown|Disconnected|Associated|Addressed) address_lost=[01]|"
    rb"CSI_(?:UDP|TCP)_FIRST_SEND call_us=\d{1,20}|"
    rb"CSI_PROGRESS elapsed_ms=\d{1,20} frames=\d{1,20} factors=\d{1,20} sent=\d{1,10} echoes=\d{1,10} ap_status=-?\d{1,10} ap_relation=[012]|"
    rb"CSI_CALLBACKS observed=\d{1,20} foreign=\d{1,20} legacy=\d{1,20} ht=\d{1,20} other=\d{1,20} last_age_ms=\d{1,39} last_mode=\d{1,3} last_width=\d{1,3} last_channel=\d{1,3} last_len=\d{1,5} last_matching=[01] dropped=\d{1,20}|"
    rb"CSI_CAPTURE_REJECT reason=(?:rf_progress|queued_stale|rf_timeout frames=\d{1,20}|(?:udp|tcp)_timeout sequence=\d{1,10} frames=\d{1,20} sent=\d{1,10} echoes=\d{1,10}|queue overflow=\d{1,20} malformed=\d{1,20} callback_dropped=\d{1,20}))"
)


def failure_detail(data):
    """Only typed fixture failures may leave the private serial buffer."""
    match = re.search(
        rb"(?:^|[\r\n])CSI_RADIO_FAIL error=(InvalidConfig|Busy|Unavailable|Timeout|Io|Protocol)(?:[\r\n]|$)",
        data,
    )
    detail = match[1].decode("ascii") if match else "unclassified"
    sdk = re.findall(
        rb"(?:^|[\r\n])CSI_SDK_ERROR status=(-?\d{1,10})(?:[\r\n]|$)", data
    )
    sdk += re.findall(
        rb"(?:^|[\r\n])CSI_START_FAILED error=SdkError\((-?\d{1,10})\)(?:[\r\n]|$)",
        data,
    )
    reasons = re.findall(
        rb"(?:^|[\r\n])CSI_CAPTURE_REJECT reason=(rf_progress|queued_stale|queue|rf_timeout|udp_timeout|tcp_timeout)(?:[ \r\n]|$)",
        data,
    )
    if sdk:
        detail += "; SDK status=" + sdk[-1].decode("ascii")
    if reasons:
        detail += "; capture=" + reasons[-1].decode("ascii")
    counts = re.findall(
        rb"CSI_CAPTURE_REJECT reason=queue overflow=(\d{1,20}) malformed=(\d{1,20}) callback_dropped=(\d{1,20})(?:[\r\n]|$)",
        data,
    )
    if counts:
        detail += "; queue counters=" + ",".join(x.decode("ascii") for x in counts[-1])
    for line in data.splitlines():
        if line.startswith(
            (
                b"NXRS_WIFI_STAGE_FAIL ",
                b"NXRS_WIFI_OBSERVATION ",
                b"CSI_UDP_FIRST_SEND ",
                b"CSI_TCP_FIRST_SEND ",
                b"CSI_IO_ERROR ",
                b"CSI_TRAFFIC ",
                b"CSI_TEST_OPTIONS ",
                b"CSI_PROGRESS ",
                b"CSI_CALLBACKS ",
                b"CSI_CAPTURE_REJECT reason=udp_timeout ",
                b"CSI_CAPTURE_REJECT reason=tcp_timeout ",
            )
        ) and DIAGNOSTIC.fullmatch(line):
            detail += "; " + line.decode("ascii")
    return detail


def capture_observations(data):
    """Bounded scalar observations only; never retain arbitrary console text."""
    result = {}
    for line in data.splitlines():
        if line.startswith(
            (b"CSI_PROGRESS ", b"CSI_CALLBACKS ")
        ) and DIAGNOSTIC.fullmatch(line):
            name, *fields = line.decode("ascii").split()
            if name in result:
                raise ValueError("duplicate capture observation")
            result[name] = {
                key: int(value)
                for key, value in (field.split("=", 1) for field in fields)
            }
    return result


def validate_report(data, served, force, transport_only=False, tcp=False):
    protocol = "tcp" if tcp else "udp"
    traffic = [line for line in data.splitlines() if line.startswith(b"CSI_TRAFFIC ")]
    if traffic != [f"CSI_TRAFFIC protocol={protocol}".encode("ascii")]:
        raise ValueError("missing, duplicate or mismatched traffic protocol")
    closed = [line for line in data.splitlines() if line.startswith(b"CSI_TCP_CLOSED ")]
    if closed != ([b"CSI_TCP_CLOSED peer_eof=1"] if tcp else []):
        raise ValueError("missing, duplicate or mismatched TCP shutdown evidence")
    if (
        b"CSI_RADIO_FAIL" in data
        or b"CSI_CLEANUP_FAILED" in data
        or b"CSI_CHECK_FAIL" in data
    ):
        raise ValueError("device reported failure; raw console withheld")
    if transport_only:
        reports = TRANSPORT_RESULT.findall(data)
        if force or len(reports) != 1 or RESULT.search(data):
            raise ValueError("missing or mixed transport control result")
        echoes = int(reports[0])
        if (
            not 10 <= echoes <= 3001
            or echoes != len(served)
            or served != set(range(echoes))
        ):
            raise ValueError("incomplete transport echo evidence")
        if (
            data.splitlines().count(
                b"CSI_TRANSPORT_PASS cleanup=confirmed station=disconnected worker=joined advisory_only=1"
            )
            != 1
        ):
            raise ValueError("transport shutdown confirmation missing")
        return dict(
            echoes=echoes,
            scope=f"{protocol.upper()} transport only; CSI not configured",
            traffic_protocol=protocol,
            csi_configured=False,
        )
    if TRANSPORT_RESULT.search(data):
        raise ValueError("transport control cannot qualify capture")
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
        data.splitlines().count(
            b"CSI_RADIO_PASS cleanup=confirmed station=disconnected worker=joined advisory_only=1"
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
        observations=capture_observations(data),
        traffic_protocol=protocol,
    )


def private_capture(path):
    path = Path(path).absolute()
    if path.resolve().is_relative_to(ROOT):
        raise ValueError("save RF captures outside the repository")
    return os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb"
    )


def validate_observation(report, seconds):
    progress = report["observations"].get("CSI_PROGRESS", {})
    callbacks = report["observations"].get("CSI_CALLBACKS", {})
    if (
        progress.get("elapsed_ms", 0) < seconds * 1000
        or progress.get("frames") != report["frames"]
        or progress.get("factors") != report["factors"]
        or progress.get("sent") != report["echoes"]
        or progress.get("echoes") != report["echoes"]
        or callbacks.get("dropped") != 0
        or callbacks.get("observed", 0) - callbacks.get("foreign", 0) < report["frames"]
        or sum(callbacks.get(k, 0) for k in ("legacy", "ht", "other")) != callbacks.get("observed")
    ):
        raise ValueError("incomplete duration, callback or progress evidence")


def validate_options(data, transport_only, raw, force, port, tcp=False):
    received = [
        m.groups() for line in data.splitlines() if (m := OPTIONS.fullmatch(line))
    ]
    expected = (int(transport_only), int(raw), int(force), port, int(tcp))
    if len(received) != 1 or tuple(map(int, received[0])) != expected:
        raise RuntimeError(
            "target options/traffic port differ; possible shell command truncation; credentials withheld"
        )


def write_console(console, payload):
    """The pinned USB-serial driver has a 64-byte receive ring and no flow control."""
    for offset in range(0, len(payload), 16):
        chunk = payload[offset : offset + 16]
        if console.write(chunk) != len(chunk):
            raise RuntimeError("incomplete console write; credentials/command withheld")
        console.flush()
        time.sleep(0.01)


def serve_tcp_echo(listener, stopped, served, maximum):
    """One bounded TCP control connection, fixed records; arbitrary reads may split/coalesce."""
    while not stopped.is_set():
        try:
            connection, _ = listener.accept()
            break
        except socket.timeout:
            continue
    else:
        return 0
    with connection:
        connection.settimeout(0.2)
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        pending = bytearray()
        while not stopped.is_set():
            try:
                data = connection.recv(16 - len(pending))
                if not data:
                    return int(bool(pending))
                pending.extend(data)
                if len(pending) < 16:
                    continue
                sequence = int.from_bytes(pending[:4], "big")
                if (len(pending) != 16 or pending[4:] != TAG
                    or sequence != len(served) or sequence >= maximum):
                    return 1
            except socket.timeout:
                continue
            try:
                connection.sendall(pending)
            except OSError:
                return 1
            served.add(sequence)
            pending.clear()
    return 0


def qualify(args, ssid, password):
    import serial

    mode = capture_mode(args)
    transport_only = getattr(args, "transport_only", False)
    tcp = getattr(args, "tcp", False)
    served = set()
    invalid = 0
    stopped = threading.Event()
    evidence = bytearray()
    pending = bytearray()
    raw_records = 0
    stage = "shell"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as echo:
        echo.bind((args.host_ip, 0))
        if tcp:
            echo.listen(1)
        echo.settimeout(0.2)

        def serve():
            nonlocal invalid
            if tcp:
                try:
                    invalid += serve_tcp_echo(echo, stopped, served, args.seconds * 10 + 1)
                except OSError:
                    if not stopped.is_set():
                        invalid += 1
                return
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
                        if DIAGNOSTIC.fullmatch(line.strip()) or line.startswith(
                            (
                                b"CSI_CAPTURE_RESULT ",
                                b"CSI_TRANSPORT_RESULT ",
                                b"CSI_TRANSPORT_PASS ",
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

                def until(marker, timeout):
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
                            raise RuntimeError(
                                "CSI trial failed: "
                                + failure_detail(bytes(evidence) + b"\n" + pending)
                                + "; host_stage="
                                + {
                                    b"NXRS_WIFI_CREDENTIALS_READY": "credentials",
                                    b"NXRS_WIFI_ASSOCIATED_DHCP": "association",
                                    b"nsh> ": "capture",
                                }.get(marker, "status")
                                + f" echoes_served={len(served)} invalid_traffic={invalid} raw_records={raw_records}; console withheld"
                            )
                        if marker in pending:
                            end = pending.index(marker) + len(marker)
                            value = bytes(pending[:end])
                            del pending[:end]
                            collect_lines(value)
                            return value
                        # Retain only bounded typed diagnostics at every stage;
                        # association failures precede the capture stage.
                        while b"\n" in pending:
                            end = pending.index(b"\n") + 1
                            line = bytes(pending[:end])
                            del pending[:end]
                            collect_lines(line)
                        pending.extend(console.read(1024))
                        if len(pending) > 8192:
                            raise RuntimeError("console line exceeded bound")
                    raise RuntimeError(
                        f"device stage deadline exceeded: stage={stage} echoes_served={len(served)} raw_records={raw_records}; "
                        + failure_detail(bytes(evidence) + b"\n" + pending)
                    )

                console.write(b"\n\n\n")
                until(b"nsh> ", 5)
                host = f"{args.host_ip}:{echo.getsockname()[1]}"
                options = (
                    (" --raw" if args.capture else "")
                    + (" --force-baseline" if args.force_baseline else "")
                    + (" --transport-only" if transport_only else "")
                    + (" --tcp" if tcp else "")
                )
                command = f"csi_radio {mode} {args.seconds} {args.bssid} {host}{options}\n".encode(
                    "ascii"
                )
                if len(command) >= 256:
                    raise ValueError(
                        "command exceeds the test image's shell line capacity"
                    )
                stage = "credentials"
                write_console(console, command)
                until(b"NXRS_WIFI_CREDENTIALS_READY", 5)
                validate_options(
                    bytes(evidence),
                    transport_only,
                    bool(args.capture),
                    args.force_baseline,
                    echo.getsockname()[1],
                    tcp,
                )
                write_console(
                    console, bytes((len(ssid), len(password))) + ssid + password
                )
                stage = "association"
                until(b"NXRS_WIFI_ASSOCIATED_DHCP", 35)
                stage = "capture"
                until(b"nsh> ", args.seconds + 15)
                stage = "status"
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
        raise RuntimeError("invalid traffic or echo worker did not stop")
    report = validate_report(
        bytes(evidence), served, args.force_baseline, transport_only, tcp
    )
    if transport_only:
        return report
    validate_observation(report, args.seconds)
    if not args.capture and report["raw_exported"] != 0:
        raise RuntimeError("unexpected raw export mode")
    if args.capture and not 1 <= report["raw_exported"] <= 3001:
        raise RuntimeError("raw export count outside bounded trial")
    if args.capture and report["raw_exported"] != raw_records:
        raise RuntimeError("RF export record count mismatch")
    return report


def capture_mode(args):
    maintenance = getattr(args, "ack_camera_maintenance", False)
    spare = getattr(args, "ack_spare_board", False)
    if maintenance == spare or (maintenance and args.force_baseline):
        raise ValueError("select one hardware mode; gain forcing requires a spare")
    if getattr(args, "transport_only", False) and (args.force_baseline or args.capture):
        raise ValueError("transport control cannot force gain or export CSI")
    return "--maintenance-camera" if maintenance else "--spare-board"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--ack-spare-board", action="store_true")
    mode.add_argument(
        "--ack-camera-maintenance",
        action="store_true",
        help="explicitly authorized camera interruption with verified backup/restore; no gain forcing",
    )
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
        "--transport-only",
        action="store_true",
        help="UDP control without CSI configure/register/enable calls",
    )
    parser.add_argument("--tcp", action="store_true", help="separate TCP control; UDP remains the baseline")
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
        capture_mode(args)
        if args.bssid != "associated" and not re.fullmatch(
            r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", args.bssid
        ):
            raise ValueError("BSSID must contain six hex octets")
        mac = (
            bytes.fromhex(args.bssid.replace(":", ""))
            if args.bssid != "associated"
            else None
        )
        if mac is not None and (mac == bytes(6) or mac[0] & 1):
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
