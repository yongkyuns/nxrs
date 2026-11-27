"""Validate Motion Tracker NDJSON and stage bounded inputs for native sim."""
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from itertools import count
import math
import struct

SOURCES = {
    "imu_primary": "imu-primary.bin",
    "imu_secondary": "imu-secondary.bin",
    "gnss": "gnss.bin",
}
ORIGIN = struct.Struct("<Q")
IMU = struct.Struct("<Q6h")
FIX = struct.Struct("<Qddfq")
NO_UTC = -(1 << 63)
EPOCH = datetime(1970, 1, 1)
MAX_LINE = 4096
ENDS = {"stopped", "disconnected", "invalid_stream", "writer_overrun", "clock_reset", "source_failure"}
INTERRUPTIONS = {"reconnecting", "backgrounded"}


@dataclass(frozen=True)
class Recording:
    origin_ms: int
    duration_ms: int
    counts: dict[str, int]
    complete: bool
    archive_version: int = 1


def integer(value, lo, hi):
    if type(value) is not int or not lo <= value <= hi:
        raise ValueError("invalid integer")
    return value


def finite(value, lo, hi):
    if type(value) not in (int, float) or not lo <= value <= hi or not math.isfinite(value):
        raise ValueError("invalid measurement")
    return value


def counts(axes, scale):
    if not isinstance(axes, list) or len(axes) != 3:
        raise ValueError("expected three axes")
    return [
        max(-32768, min(32767, round(finite(value, -32768.5 * scale, 32767.5 * scale) / scale)))
        for value in axes
    ]


def rows(stream):
    for number in count(1):
        raw = stream.readline(MAX_LINE + 1)
        if not raw:
            return
        if len(raw) > MAX_LINE or not raw.endswith(b"\n"):
            raise ValueError(f"line {number}: oversized or incomplete record")
        try:
            row = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError(f"line {number}: invalid JSON") from error
        if not isinstance(row, dict):
            raise ValueError(f"line {number}: expected an object")
        yield number, row


def stage(path, fixture):
    """Stage v1 historical arrival schedules or complete v2 capture histories.

    The native fixture cannot preserve original receipt-only timing, no-fix
    events, or terminal failures. Reject those v2 histories rather than turn
    them into capture evidence or silently discard lifecycle records.
    """
    seen = dict.fromkeys(SOURCES, 0)
    previous_time = {}
    previous_sequence = {}
    paused = end = incomplete = False
    first = last = None
    with ExitStack() as stack:
        stream = stack.enter_context(path.open("rb"))
        entries = rows(stream)
        _, header = next(entries, (0, {}))
        version = header.get("version")
        if (type(version) is not int or version not in (1, 2)
                or header != {"type": "header", "format": "nxrs-motion-tracker", "version": version}):
            raise ValueError("expected a Motion Tracker v1 or v2 header")
        outputs = {source: stack.enter_context((fixture / name).open("wb"))
                   for source, name in SOURCES.items()}
        for source in ("imu_primary", "imu_secondary"):
            outputs[source].write(ORIGIN.pack(0))

        for number, row in entries:
            try:
                kind = row.get("type")
                if end:
                    raise ValueError("records after the footer")
                if kind == "end":
                    end = True
                    reason = row.get("reason")
                    complete = row.get("complete")
                    if reason not in ENDS or (version == 1 and reason == "source_failure") or type(complete) is not bool:
                        raise ValueError("invalid footer")
                    if version == 2 and not complete:
                        raise ValueError("native replay requires a complete v2 recording")
                    if complete and (reason != "stopped" or incomplete or first is None):
                        raise ValueError("footer contradicts loss or interruption")
                    continue
                if version == 2 and kind == "source_failure":
                    raise ValueError("native replay cannot preserve a v2 source_failure history")
                if version == 2 and kind == "no_fix":
                    raise ValueError("native replay cannot preserve v2 no_fix lifecycle records")
                if kind == "interruption" and not paused and row.get("reason") in INTERRUPTIONS:
                    paused = incomplete = True
                    continue
                if kind == "resume" and paused:
                    paused = False
                    previous_sequence.clear()
                    continue
                source = row.get("source")
                if paused or source not in SOURCES or kind != ("gnss" if source == "gnss" else "imu"):
                    raise ValueError("unexpected sample or transition")
                if version == 2:
                    provenance = row.get("timestamp_provenance")
                    if provenance not in ("capture", "received", "unknown"):
                        raise ValueError("invalid v2 timestamp provenance")
                    if provenance != "capture":
                        raise ValueError(f"native replay requires capture-qualified v2 timestamps; {provenance} has unknown capture age")
                    if "received_ms" in row:
                        raise ValueError("ambiguous v2 timestamp fields")
                stamp = integer(row["timestamp_ms" if version == 2 else "received_ms"], 0, (1 << 64) - 1)
                seq = integer(row["sequence"], 0, (1 << 32) - 1)
                missing = integer(row.get("missed_before", 0), 0, (1 << 31) - 2)
                if source in previous_sequence:
                    step = (seq - previous_sequence[source]) & 0xffffffff
                    if not (0 < step < 0x80000000) or missing != step - 1:
                        raise ValueError("sequence and gap disagree")
                elif missing:
                    raise ValueError("gap without a preceding sample")
                if stamp < previous_time.get(source, 0):
                    raise ValueError("tracker time moved backwards")
                utc = row.get("utc_ms")
                if utc is not None:
                    utc = integer(utc, -62135596800000, 253402300799999)
                if source == "gnss":
                    payload = FIX.pack(
                        stamp, finite(row["lat_deg"], -90, 90), finite(row["lon_deg"], -180, 180),
                        finite(row["speed_mps"], 0, 2_147_483.5), utc if utc is not None else NO_UTC,
                    )
                else:
                    gyro = counts(row["gyro_rps"], math.radians(2000) / 32768)
                    accel = counts(row["accel_mps2"], 9.80665 * 4 / 32768)
                    payload = IMU.pack(stamp, *gyro, *accel)
                outputs[source].write(payload)
                previous_time[source] = stamp
                previous_sequence[source] = seq
                seen[source] += 1
                incomplete |= bool(missing)
                first = stamp if first is None else min(first, stamp)
                last = stamp if last is None else max(last, stamp)
            except (KeyError, TypeError, ValueError, OverflowError) as error:
                raise ValueError(f"line {number}: {error}") from error

        if not end or first is None:
            raise ValueError("recording needs samples and a completed footer")
        for source in ("imu_primary", "imu_secondary"):
            outputs[source].seek(0)
            outputs[source].write(ORIGIN.pack(first))
        return Recording(first, last - first, seen, complete, version)


def fixes(path):
    with path.open("rb") as stream:
        while raw := stream.read(FIX.size):
            stamp, latitude, longitude, speed, utc = FIX.unpack(raw)
            yield stamp, latitude, longitude, speed, (
                None if utc == NO_UTC else EPOCH + timedelta(milliseconds=utc))
