"""Physical-camera evidence parser controls; these are not hardware results."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('camera_capture', ROOT / 'tests/nuttx-std/camera/run.py')
CAPTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CAPTURE)


def evidence(raw=None):
    # Little-endian RGB565 red, green, blue, then black pixels.
    if raw is None:
        raw = bytes.fromhex('00f8e0071f00') + bytes(CAPTURE.FRAME_BYTES - 6)
    checksum = 2166136261
    for byte in raw:
        checksum = ((checksum ^ byte) * 16777619) & 0xffffffff
    frames = b''.join(
        f'NXRS_CAMERA_FRAME cycle={c} sequence={s} bytes={len(raw)} checksum={checksum:08x}\r\n'.encode()
        for c in range(3) for s in range(3))
    body = raw.hex().encode()
    return (frames + f'NXRS_CAMERA_HEX_BEGIN bytes={len(raw)}\r\n'.encode()
            + b'\r\n'.join(body[i:i+256] for i in range(0, len(body), 256))
            + b'\r\nNXRS_CAMERA_HEX_END\r\n' + CAPTURE.PASS)


class CameraEvidenceTests(unittest.TestCase):
    def test_jpeg_dump_keeps_actual_variable_length_and_checksum(self):
        # Framing/parser control only, deliberately not a JPEG codec fixture.
        raw = bytes.fromhex('ffd8ffda000244ff00d9ffd9')
        self.assertEqual(CAPTURE.frame_bytes(evidence(raw), jpeg=True), raw)
        with self.assertRaises(ValueError):
            CAPTURE.frame_bytes(evidence(raw), jpeg=False)
        for broken in (raw[:-1], b'\x00' + raw[1:], bytes(CAPTURE.FRAME_BYTES + 1)):
            with self.assertRaises(ValueError):
                CAPTURE.frame_bytes(evidence(broken), jpeg=True)

    def test_heap_snapshots_require_both_complete_heaps(self):
        table = (b' total used free maxused maxfree nused nfree name\r\n'
                 b' 379744 23080 356664 23448 351664 71 4 Kmem\r\n'
                 b' 8388608 8192 8380416 323584 8300000 4 2 Umem\r\n')
        self.assertEqual(CAPTURE.parse_heap(table), {'Kmem': 23080, 'Umem': 8192})
        for broken in (b'', table + table, table.replace(b'Kmem', b'Other')):
            with self.assertRaises(ValueError):
                CAPTURE.parse_heap(broken)

    def test_complete_frame_normalizes_rgb565_to_visible_rgb(self):
        image = CAPTURE.decode(evidence())
        header = b'P6\n320 240\n255\n'
        self.assertTrue(image.startswith(header))
        self.assertEqual(len(image), len(header) + 320 * 240 * 3)
        self.assertEqual(image[len(header):len(header)+9], bytes.fromhex('ff000000ff000000ff'))

    def test_incomplete_duplicate_corrupt_or_failed_evidence_is_rejected(self):
        valid = evidence()
        for broken in (
            valid.replace(CAPTURE.PASS, b''),
            valid + b'\nNXRS_CAMERA_FAIL error=Io',
            valid.replace(b'cycle=1 sequence=0', b'cycle=0 sequence=0'),
            valid.replace(b'bytes=153600', b'bytes=153599', 1),
            valid.replace(b'00f8e0071f00', b'10f8e0071f00'),
            valid.replace(b'NXRS_CAMERA_HEX_END', b''),
            valid.replace(b'NXRS_CAMERA_HEX_END', b'?NXRS_CAMERA_HEX_END'),
            valid + valid,
        ):
            with self.subTest(length=len(broken)):
                with self.assertRaises(ValueError):
                    CAPTURE.decode(broken)


if __name__ == '__main__':
    unittest.main()
