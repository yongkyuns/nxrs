"""Shared UBX receiver model and tracker telemetry checks."""
import struct
from datetime import datetime, timedelta


def ubx(kind, identifier, payload):
    body = struct.pack('<BBH', kind, identifier, len(payload)) + payload
    a = b = 0
    for byte in body:
        a = (a + byte) & 255
        b = (b + a) & 255
    return bytes((0xb5, 0x62)) + body + bytes((a, b))


class NeoM9n:
    KEY = bytes((7, 0, 0x91, 0x20))
    SATELLITE_KEY = bytes((0x16, 0, 0x91, 0x20))

    def __init__(self, wire):
        self.wire = wire
        self.buffer = bytearray()
        self.rate = 7
        self.satellite_rate = 0
        self.navigation = (1000, 1)
        self.writes = []
        self.fix_sent = False

    def receive(self, chunk):
        self.buffer.extend(chunk)
        while len(self.buffer) >= 6:
            assert self.buffer[:3] == bytes((0xb5, 0x62, 6)), self.buffer.hex()
            identifier = self.buffer[3]
            length = int.from_bytes(self.buffer[4:6], 'little')
            assert length in (8, 9, 12, 16), length
            if len(self.buffer) < length + 8:
                break
            data = bytes(self.buffer[6:6 + length])
            assert self.buffer[:length + 8] == ubx(6, identifier, data)
            del self.buffer[:length + 8]
            if length in (12, 16):
                measurement, cycles = struct.pack('<I', 0x30210001), struct.pack('<I', 0x30210002)
                if identifier == 0x8a:
                    assert length == 16 and data[:8] == bytes((0, 1, 0, 0)) + measurement
                    assert data[10:14] == cycles
                    self.navigation = struct.unpack('<H', data[8:10])[0], struct.unpack('<H', data[14:16])[0]
                    self.wire.sendall(ubx(5, 1, bytes((6, 0x8a))))
                else:
                    assert identifier == 0x8b and data == bytes(4) + measurement + cycles
                    first, second = (struct.pack('<H', number) for number in self.navigation)
                    self.wire.sendall(ubx(6, 0x8b, bytes((1, 0, 0, 0)) + measurement + first + cycles + second))
                continue
            key = data[4:8]
            assert key in (self.KEY, self.SATELLITE_KEY)
            if identifier == 0x8a:
                assert data[:4] == b'\0\1\0\0'
                if key == self.KEY:
                    self.rate = data[-1]
                    self.writes.append(self.rate)
                else:
                    self.satellite_rate = data[-1]
                self.wire.sendall(ubx(5, 1, bytes((6, 0x8a))))
            else:
                assert identifier == 0x8b and data[:4] == bytes(4)
                rate = self.rate if key == self.KEY else self.satellite_rate
                answer = ubx(6, 0x8b, b'\1\0\0\0' + key + bytes((rate,)))
                self.wire.sendall(answer)

    def send_fix(self):
        assert self.rate == 1 and not self.fix_sent
        self.wire.sendall(self.fix_packet(1000))
        self.fix_sent = True

    @staticmethod
    def satellite_packet(tow_ms, signals):
        payload = struct.pack('<IBB2x', tow_ms, 1, len(signals))
        for system, satellite, strength in signals:
            payload += struct.pack('<BBB5xI', system, satellite, strength, 4)
        return ubx(1, 0x35, payload)

    @staticmethod
    def fix_packet(tow_ms, longitude_deg=-11.5166667, speed_mps=0, latitude_deg=48.1173,
                   *, utc=None, replay=False):
        position = bytearray(92)
        struct.pack_into('<I', position, 0, tow_ms)
        if not replay:
            utc = datetime(2026, 1, 1) + timedelta(milliseconds=tow_ms)
        if utc is not None:
            struct.pack_into('<H6B', position, 4,
                             utc.year, utc.month, utc.day, utc.hour, utc.minute, utc.second, 7)
            struct.pack_into('<i', position, 16, utc.microsecond * 1000)
        position[20:22] = b'\3\1'
        struct.pack_into('<ii', position, 24, round(longitude_deg * 10_000_000),
                         round(latitude_deg * 10_000_000))
        struct.pack_into('<i', position, 60, round(speed_mps * 1000))
        if replay:
            struct.pack_into('<I', position, 72, 18_000_000)  # Heading unspecified.
        return ubx(1, 7, position)


def check_imus(sample):
    assert sample['v'] == 1, sample
    imu = sample['imu']
    # Receipt-fresh data is usable here without asserting known capture age.
    assert len(imu) == 2 and all(source['state'] in ('live', 'received') for source in imu), sample
    assert imu[0]['accel_mps2'][0] < 0 < imu[1]['accel_mps2'][0], sample


def check_live(sample, **location):
    check_imus(sample)
    check_fix(sample, **location)


def check_fix(sample, *, latitude_deg=48.1173, longitude_deg=-11.5166667):
    gnss = sample['gnss']
    assert gnss['state'] in ('live', 'received'), sample
    assert abs(gnss['lat_deg'] - latitude_deg) < 1e-6 and abs(gnss['lon_deg'] - longitude_deg) < 1e-6, sample


def check_stale(sample):
    check_imus(sample)
    assert sample['gnss'] == {'state': 'stale'}, sample


def check_progress(samples):
    for previous, latest in zip(samples, samples[1:]):
        assert latest['gnss']['seq'] > previous['gnss']['seq'], samples
        for old, new in zip(previous['imu'], latest['imu']):
            if old['state'] in ('live', 'received') and new['state'] in ('live', 'received'):
                assert new['seq'] > old['seq'], samples
