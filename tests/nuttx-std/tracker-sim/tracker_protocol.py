"""Independent, bounded decoder for Motion Tracker export versions 1 through 4.

This module has no Bluetooth, simulator or image dependencies. Measurements keep
the legacy five-field iteration shape used by qualification counters, with
explicit timestamp provenance available as an attribute. NoFix is a distinct
lifecycle record and must not be counted as a missing or valid measurement.
"""
from dataclasses import dataclass
import math
import struct


CAUSES = {
    1: 'unsupported', 2: 'busy', 3: 'timeout', 4: 'fifo_overflow',
    5: 'io', 6: 'invalid_data', 7: 'lost_availability_evidence',
}


@dataclass(frozen=True)
class Measurement:
    source: int
    sequence: int
    timestamp_ms: int
    utc_ms: int | None
    values: tuple
    provenance: str

    def __iter__(self):
        return iter((self.source, self.sequence, self.timestamp_ms, self.utc_ms, self.values))


@dataclass(frozen=True)
class NoFix:
    generation: int
    received_ms: int
    source_epoch_ms: int | None


class CaptureFault(ValueError):
    def __init__(self, source, cause):
        self.source, self.cause = source, cause
        self.reason = CAUSES[cause]
        super().__init__(f'tracker source {source} failed: {self.reason}')


class CaptureOverflow(ValueError):
    def __init__(self, source=None, sequence=None):
        self.source, self.sequence = source, sequence
        super().__init__(f'tracker export buffer overflow: source={source}, first_missing={sequence}')


def preferred_version(capabilities):
    advertised = capabilities.get('export_version') or 1
    if advertised >= 4:
        return 4
    if advertised >= 3 or len(capabilities.get('imu_scale_bits') or []) == 2:
        return 3
    return 2 if advertised == 2 else 1


def decode_frame(frame, scales=(None, None)):
    if len(frame) < 4 or frame[0] != 0xa5:
        raise ValueError('invalid tracker frame header')
    size = int.from_bytes(frame[2:4], 'little')
    if len(frame) != size + 4:
        raise ValueError('invalid tracker frame length')
    return decode_batch(frame[4:], frame[1], scales)


def decode_batch(payload, version, scales):
    if version not in (1, 2, 3, 4):
        raise ValueError('unsupported tracker export version')
    if version == 4 and len(payload) == 2:
        source, cause = payload
        if source not in (1, 2, 3) or cause not in CAUSES:
            raise ValueError('invalid tracker source fault')
        raise CaptureFault(source, cause)
    if version == 1 and not payload:
        raise CaptureOverflow()
    if version >= 2 and len(payload) == 5:
        source, sequence = struct.unpack('<BI', payload)
        if source not in (1, 2, 3):
            raise ValueError('invalid tracker overflow source')
        raise CaptureOverflow(source, sequence)
    minimum = 33 if version == 3 else 29
    maximum = 600 if version >= 3 else 528
    if not minimum <= len(payload) <= maximum:
        raise ValueError('invalid tracker batch length')
    timed = version >= 3
    utc_offset = struct.unpack_from('<q', payload)[0] if timed else -(1 << 63)
    previous = {}
    records = []
    pos = 8 if timed else 0
    while pos < len(payload):
        if len(records) >= 16:
            raise ValueError('too many tracker records')
        tag = payload[pos]
        if version == 4 and tag & 0x0f == 4:
            if tag != 4 or pos + 21 > len(payload):
                raise ValueError('invalid no-fix flags or length')
            generation, received, epoch = struct.unpack_from('<IQQ', payload, pos + 1)
            records.append(NoFix(generation, received, None if epoch == (1 << 64) - 1 else epoch))
            pos += 21
            continue
        delta = version >= 2 and tag & 0x80 != 0
        counts = timed and tag & 0x40 != 0
        source = tag & (0x0f if version == 4 else 0x1f if timed else 0x7f if delta else 0xff)
        if source not in (1, 2, 3) or (source == 3 and counts):
            raise ValueError('invalid tracker kind or flags')
        # V4 UTC is capture-qualified; receipt-only records must carry NO_UTC.
        if version == 4 and tag & 0x10 and not tag & 0x20:
            raise ValueError('receipt timestamp without NO_UTC')
        header = 2 if delta else 13 if timed else 9
        end = pos + header + (20 if source == 3 else 12 if counts else 24)
        if end > len(payload):
            raise ValueError('partial tracker record')
        if delta:
            if source not in previous:
                raise ValueError('orphan tracker delta')
            sequence, stamp = previous[source]
            sequence = (sequence + 1) & 0xffffffff
            stamp += payload[pos + 1]
            if timed and stamp >= 1 << 64:
                raise ValueError('tracker timestamp overflow')
            if not timed:
                stamp &= 0xffffffff
        else:
            sequence, stamp = struct.unpack_from('<IQ' if timed else '<II', payload, pos + 1)
        previous[source] = sequence, stamp
        utc = None
        if utc_offset != -(1 << 63) and not tag & 0x20:
            utc = stamp + utc_offset
            if stamp >= 1 << 63 or not -(1 << 63) <= utc < 1 << 63:
                raise ValueError('tracker UTC overflow')
        offset = pos + header
        if counts:
            scale = scales[source - 1] if len(scales) > source - 1 else None
            if scale is None or len(scale) != 2 or not all(math.isfinite(x) and x > 0 for x in scale):
                raise ValueError('missing or invalid IMU scale')
            points = struct.unpack_from('<6h', payload, offset)
            try:
                values = tuple(struct.unpack('<f', struct.pack('<f', point * scale[axis // 3]))[0]
                               for axis, point in enumerate(points))
            except OverflowError as error:
                raise ValueError('invalid scaled IMU value') from error
        else:
            values = struct.unpack_from('<ddf' if source == 3 else '<6f', payload, offset)
        if not all(math.isfinite(value) for value in values):
            raise ValueError('nonfinite tracker value')
        if source == 3 and not (-90 <= values[0] <= 90 and -180 <= values[1] <= 180 and values[2] >= 0):
            raise ValueError('invalid tracker fix')
        provenance = ('received' if tag & 0x10 else 'capture') if version == 4 else 'unknown'
        records.append(Measurement(source, sequence, stamp, utc, values, provenance))
        pos = end
    return records
