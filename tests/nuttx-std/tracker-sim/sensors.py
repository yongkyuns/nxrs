"""Shared UBX receiver model and tracker telemetry checks."""
import struct


def ubx(kind, identifier, payload):
    body = struct.pack('<BBH', kind, identifier, len(payload)) + payload
    a = b = 0
    for byte in body:
        a = (a + byte) & 255
        b = (b + a) & 255
    return bytes((0xb5, 0x62)) + body + bytes((a, b))


class NeoM9n:
    KEY = bytes((7, 0, 0x91, 0x20))

    def __init__(self, wire):
        self.wire = wire
        self.buffer = bytearray()
        self.rate = 7
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
            if identifier == 0x8a:
                assert data[:-1] == b'\0\1\0\0' + self.KEY
                self.rate = data[-1]
                self.writes.append(self.rate)
                self.wire.sendall(ubx(5, 1, bytes((6, 0x8a))))
            else:
                assert identifier == 0x8b and data == bytes(4) + self.KEY
                answer = ubx(6, 0x8b, b'\1\0\0\0' + self.KEY + bytes((self.rate,)))
                self.wire.sendall(answer)

    def send_fix(self):
        assert self.rate == 1 and not self.fix_sent
        self.wire.sendall(self.fix_packet(1000))
        self.fix_sent = True

    @staticmethod
    def fix_packet(tow_ms):
        position = bytearray(92)
        struct.pack_into('<I', position, 0, tow_ms)
        position[20:22] = b'\3\1'
        struct.pack_into('<ii', position, 24, -115166667, 481173000)
        return ubx(1, 7, position)


def check_imus(sample):
    assert sample['v'] == 1, sample
    imu = sample['imu']
    assert len(imu) == 2 and all(source['state'] == 'live' for source in imu), sample
    assert imu[0]['accel_mps2'][0] < 0 < imu[1]['accel_mps2'][0], sample


def check_live(sample):
    check_imus(sample)
    check_fix(sample)


def check_fix(sample):
    gnss = sample['gnss']
    assert gnss['state'] == 'live', sample
    assert abs(gnss['lat_deg'] - 48.1173) < 1e-6 and abs(gnss['lon_deg'] + 11.5166667) < 1e-6, sample


def check_stale(sample):
    check_imus(sample)
    assert sample['gnss'] == {'state': 'stale'}, sample


def check_progress(samples):
    for previous, latest in zip(samples, samples[1:]):
        assert latest['gnss']['seq'] > previous['gnss']['seq'], samples
        for old, new in zip(previous['imu'], latest['imu']):
            if old['state'] == new['state'] == 'live':
                assert new['seq'] > old['seq'], samples
