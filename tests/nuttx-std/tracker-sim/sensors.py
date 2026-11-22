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
        self.writes = []
        self.fix_sent = False

    def receive(self, chunk):
        self.buffer.extend(chunk)
        while len(self.buffer) >= 6:
            assert self.buffer[:3] == bytes((0xb5, 0x62, 6)), self.buffer.hex()
            identifier = self.buffer[3]
            length = int.from_bytes(self.buffer[4:6], 'little')
            assert length in (8, 9), length
            if len(self.buffer) < length + 8:
                break
            data = bytes(self.buffer[6:6 + length])
            assert self.buffer[:length + 8] == ubx(6, identifier, data)
            del self.buffer[:length + 8]
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
    gnss = sample['gnss']
    assert gnss['state'] == 'live', sample
    assert abs(gnss['lat_deg'] - 48.1173) < 1e-6 and abs(gnss['lon_deg'] + 11.5166667) < 1e-6, sample


def check_stale(sample):
    check_imus(sample)
    assert sample['gnss'] == {'state': 'stale'}, sample
