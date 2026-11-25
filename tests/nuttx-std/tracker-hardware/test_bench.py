import json
import unittest

from bench import reading, summarize, summary_time


class BenchResults(unittest.TestCase):
    def setUp(self):
        self.ble = [{"event": "ble_connected"}] + [
            {"event": "ble", "host_s": n, "imu": [[n, 1000 * n]] * 2}
            for n in range(1, 17)
        ]
        self.uploads = [
            {"session": 1, "received_ms": 1000},
            {"session": 2, "received_ms": 10000},
        ]

    def summary(self):
        return summarize(self.ble, self.uploads, loss_ms=4000, restored_ms=8000)

    def test_recovery(self):
        self.assertEqual(self.summary(), ([4, 4, 8], 1, 2, 2))

    def test_rejects_ble_pause(self):
        for entry in self.ble[7:]:
            entry["host_s"] += 3.8
        with self.assertRaisesRegex(ValueError, "max host gap"):
            self.summary()

    def test_rejects_ble_reconnection(self):
        self.ble.insert(7, {"event": "ble_connected"})
        with self.assertRaisesRegex(ValueError, "single authenticated BLE session"):
            self.summary()

    def test_rejects_replay_or_reuse_of_the_original_tls_connection(self):
        for upload in ({"session": 2, "received_ms": 8000}, self.uploads[1] | {"session": 1}):
            with self.subTest(upload=upload):
                self.uploads[1] = upload
                with self.assertRaisesRegex(ValueError, "fresh telemetry"):
                    self.summary()

    def test_summary_logs_only_imu1_time_when_both_imus_are_live(self):
        frame = {
            "v": 1, "kind": "summary", "imu": ["live", "live"], "imu1_received_ms": 500,
            "gnss": {"state": "live", "lat_deg": 0.25, "lon_deg": -1.25},
        }
        self.assertEqual(summary_time((json.dumps(frame) + "\n").encode()), 500)
        frame["imu"][1] = "stale"
        self.assertIsNone(summary_time((json.dumps(frame) + "\n").encode()))

    def test_logs_only_imu_metadata(self):
        frame = {
            "v": 1,
            "imu": [{"state": "live", "seq": 1, "received_ms": 500}, {"state": "stale"}],
            "gnss": {"state": "live", "lat_deg": 0.25, "lon_deg": -1.25},
        }
        self.assertEqual(reading((json.dumps(frame) + "\n").encode()), [[1, 500], None])


if __name__ == "__main__":
    unittest.main()
