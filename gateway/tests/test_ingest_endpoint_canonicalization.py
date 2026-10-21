from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'deploy'))
import ingest_service


class CanonicalEndpointTests(unittest.TestCase):
    def test_port_normalization_cannot_bypass_publisher_collision(self):
        for port in ['8554', '08554', '0008554']:
            with self.subTest(port=port), self.assertRaises(ValueError):
                ingest_service.configuration('127.0.0.1:'+port, acknowledge_http=True)
        self.assertEqual(ingest_service.configuration('192.168.1.50:08080', acknowledge_http=True)['camera'],
                         '192.168.1.50:8080')


if __name__ == '__main__':
    unittest.main()
