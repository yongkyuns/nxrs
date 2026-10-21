#!/usr/bin/env python3
"""Optional installed-vendor-driver baseline; reset the ESP before each run."""
import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import socket
import time

SPEC = importlib.util.spec_from_file_location("stream_runner", Path(__file__).parents[1] / "run.py")
STREAM = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STREAM)


def run(args):
    import serial
    from PIL import Image

    ssid, password = STREAM.read_wifi_file(args.wifi_file)
    report = STREAM._new_file(args.report, ".json")
    output = STREAM._new_file(args.output, ".jpg")
    if report.exists() or output.exists():
        raise ValueError("use new private output paths")
    pending = bytearray()
    console = serial.Serial()
    console.port, console.baudrate, console.timeout = args.port, 115200, 0.1
    console.write_timeout = 2
    console.dtr = console.rts = False
    console.open()

    def line(marker, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pending.extend(console.read(512))
            if len(pending) > 8192 or password and password in pending:
                raise RuntimeError("serial bound or credential echo violation; raw output withheld")
            if b"NXRS_REFERENCE_FAIL" in pending:
                raise RuntimeError("reference firmware reported failure; raw output withheld")
            start = pending.find(marker)
            end = pending.find(b"\n", start) if start >= 0 else -1
            if end >= 0:
                result = bytes(pending[start:end]).rstrip(b"\r")
                del pending[:end + 1]
                return result
        raise RuntimeError("reference stage timed out; raw output withheld")

    with console:
        line(b"NXRS_REFERENCE_READY", 10)
        console.write(f"{args.width} {args.height} {args.xclk} {args.quality} {args.buffers}\n".encode())
        capture = line(b"NXRS_REFERENCE_CAPTURE", 45)
        match = re.fullmatch(rb"NXRS_REFERENCE_CAPTURE frames=(\d+) elapsed_ms=(\d+) bytes=(\d+) max_bytes=(\d+)", capture)
        if not match or int(match[1]) == 0 or int(match[2]) == 0:
            raise RuntimeError("invalid capture report")
        captured = dict(zip(("frames", "elapsed_ms", "bytes", "max_bytes"), map(int, match.groups())))
        sensor = line(b"NXRS_REFERENCE_SENSOR", 5)
        values = re.fullmatch(rb"NXRS_REFERENCE_SENSOR pll0=(\d+) pll1=(\d+) pll2=(\d+) pll3=(\d+) pclk=(\d+) hts=(\d+) vts=(\d+) exposure=(\d+)", sensor)
        if not values:
            raise RuntimeError("invalid sensor register report")
        sensor_registers = dict(zip(("pll0", "pll1", "pll2", "pll3", "pclk", "hts", "vts", "exposure"),
                                    map(int, values.groups())))
        line(b"NXRS_WIFI_CREDENTIALS_READY", 5)
        console.write(bytes((len(ssid), len(password))) + ssid + password)
        online = line(b"NXRS_STREAM_ONLINE", 40)
        endpoint = STREAM.ONLINE_RE.fullmatch(online)
        if not endpoint:
            raise RuntimeError("invalid endpoint report")
        host, port = endpoint[1].decode(), int(endpoint[2])
        offset, uncertainty = STREAM.clock_sample(host, port, source_ip=args.host_ip)
        count, payload, first, last, maximum_gap = 0, 0, None, None, 0
        latency_sum, latency_max = 0, 0
        with socket.create_connection((host, port), timeout=2,
                                      source_address=(args.host_ip, 0)) as connection:
            connection.settimeout(3)
            connection.sendall(b"GET /stream HTTP/1.1\r\nHost: nxrs\r\nConnection: close\r\n\r\n")
            with connection.makefile("rb") as incoming:
                STREAM.read_mjpeg_response(incoming)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    sequence, observed_ms, body = STREAM.read_mjpeg_frame(incoming)
                    with Image.open(io.BytesIO(body)) as image:
                        if image.format != "JPEG" or image.size != (args.width, args.height):
                            raise ValueError("unexpected reference image format")
                        image.load()
                    now = time.monotonic() * 1000
                    if first is None:
                        first = now
                        # Preserve exactly one private image, as in the NuttX test.
                        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(fd, "wb") as image_file:
                            image_file.write(body)
                    if last is not None:
                        maximum_gap = max(maximum_gap, now - last)
                    if sequence != count + 1:
                        raise ValueError("reference sequence mismatch")
                    last = now
                    count += 1
                    payload += len(body)
                    latency = max(0, now + offset - observed_ms)
                    latency_sum += latency
                    latency_max = max(latency_max, latency)
        line(b"NXRS_REFERENCE_PASS stopped=1", 40)
    if count < 2:
        raise RuntimeError("insufficient reference frames")
    result = {
        "result": "PASS", "driver": "installed esp32 Arduino core 2.0.5",
        "width": args.width, "height": args.height, "xclk_hz": args.xclk,
        "quality_scale": args.quality, "capture_buffers": args.buffers,
        "sensor_registers_after_capture": sensor_registers,
        "capture_only": captured, "capture_only_fps": round(captured["frames"] * 1000 / captured["elapsed_ms"], 2),
        "decoded_frames": count, "delivered_fps": round((count - 1) * 1000 / (last - first), 2),
        "mean_jpeg_bytes": round(payload / count, 1), "max_inter_frame_gap_ms": round(maximum_gap, 2),
        "observation_to_host_decode_ms": {"mean": round(latency_sum / count, 2), "max": round(latency_max, 2)},
        "clock_uncertainty_ms": uncertainty,
        "limits": "reference application uses installed unmodified vendor camera/Wi-Fi drivers; no exposure/display latency or endurance claim",
    }
    STREAM._private_report(report, result)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--host-ip", required=True)
    parser.add_argument("--wifi-file", type=Path, required=True)
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=240)
    parser.add_argument("--xclk", type=int, default=16000000)
    parser.add_argument("--quality", type=int, default=12)
    parser.add_argument("--buffers", type=int, default=2)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args())
