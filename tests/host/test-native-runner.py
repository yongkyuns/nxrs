"""Execute the real native CLI and independently verify disk + UDP artifacts."""
from pathlib import Path
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile


def checksum(payload: bytes) -> int:
    value = 2166136261
    for byte in payload:
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return value


def main() -> None:
    values = (7, 11, 23, 37)
    with tempfile.TemporaryDirectory(prefix="nxrs-native-e2e-") as root:
        root = Path(root)
        source = root / "source.gray"
        source.write_bytes(b"".join(bytes([value]) * 4 for value in values))
        output = root / "recordings"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
            receiver.bind(("127.0.0.1", 0))
            receiver.settimeout(10)
            peer = f"127.0.0.1:{receiver.getsockname()[1]}"
            result = subprocess.run(
                ["cargo", "run", "--locked", "-p", "nxrs-applications",
                 "--bin", "nxrs", "--",
                 str(source), "2", "2", "gray8", "10", str(output), peer],
                check=False, text=True, capture_output=True, timeout=120,
                env={**os.environ, "NXRS_PLATFORM": {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")},
            )
            print(result.stdout, end="")
            if result.returncode:
                print(result.stderr, file=sys.stderr, end="")
                raise RuntimeError(f"native runner exited with {result.returncode:#x}")
            runtime = re.search(
                r"^Runtime owner: camera_polls=(\d+) timed_waits=(\d+) busy_retries=(\d+)$",
                result.stdout,
                re.MULTILINE,
            )
            assert runtime, "missing event-driven owner report"
            camera_polls, timed_waits, _busy_retries = map(int, runtime.groups())
            assert camera_polls == len(values), (
                "replay should poll exactly once per frame instead of on a fixed 1 ms loop",
                camera_polls,
            )
            assert timed_waits > 0, "product owner never used a timed wait"
            packets = {}
            for _ in values:
                packet = receiver.recv(65535)
                assert len(packet) == 28, len(packet)
                version, pixels, width, height, reserved, sequence, time_ms, digest = struct.unpack("<BBHHHQQI", packet)
                assert (version, pixels, width, height, reserved) == (1, 0, 2, 2, 0)
                assert sequence not in packets, "duplicate datagram"
                packets[sequence] = (time_ms, digest)
        records = sorted(output.glob("*.rcam"))
        assert len(records) == len(values), records
        assert not list(output.glob("*.part")), "temporary record was not cleaned up"
        previous_time = None
        for expected_sequence, (path, value) in enumerate(zip(records, values), 1):
            raw = path.read_bytes()
            assert len(raw) == 44
            magic, width, height, pixels, sequence, time_ms, length = struct.unpack("<8sHHB3xQQQ", raw[:40])
            assert magic == b"RCAMREC1"
            assert raw[13:16] == b"\0\0\0"
            assert (width, height, pixels, sequence, length) == (2, 2, 0, expected_sequence, 4)
            assert path.name == f"{sequence:020}.rcam"
            assert raw[40:] == bytes([value]) * 4
            assert packets[sequence] == (time_ms, checksum(raw[40:]))
            if previous_time is not None:
                assert time_ms - previous_time == 10
            previous_time = time_ms
        print("PASS: independent decoder verified four persisted frames and four actual UDP datagrams")


if __name__ == "__main__":
    main()
