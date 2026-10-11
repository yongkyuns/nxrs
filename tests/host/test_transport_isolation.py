"""Transport dependency and received-datagram regression controls."""
import copy
import importlib.util
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[2] / 'tools/check-transport-isolation.py'
SPEC = importlib.util.spec_from_file_location('transport_isolation', SOURCE)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class PacketOracleTests(unittest.TestCase):
    def test_missing_extra_reordered_corrupted_and_wrong_source_are_rejected(self):
        good = [{'hex': '007f80ff', 'peer': ['127.0.0.1', 12345]},
                {'hex': b'transport-only'.hex(), 'peer': ['127.0.0.1', 12345]}]
        CHECK.check_packets(good)
        bad = [good[:1], good + good[:1], list(reversed(good))]
        for field, value in [('hex', '007f80'), ('hex', '007f8000'), ('peer', ['127.0.0.1', 9999])]:
            changed = copy.deepcopy(good)
            changed[0][field] = value
            bad.append(changed)
        for peer in [['192.0.2.1', 12345], ['127.0.0.1', 0]]:
            changed = copy.deepcopy(good)
            for packet in changed:
                packet['peer'] = peer
            bad.append(changed)
        for packets in bad:
            with self.subTest(packets=packets), self.assertRaises(AssertionError):
                CHECK.check_packets(packets)


if __name__ == '__main__':
    unittest.main()
