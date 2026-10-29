"""Unlabelled native-training workload gates; no device or room state required."""
import importlib.util
from pathlib import Path
import unittest
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('training_load', ROOT / 'tests/csi-radio/presence/training_load.py')
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

def probe(n):
    return dict(kind='numerical_probe', windows=n, bad_input=0, bad_output=0, kernel_errors=0, uninspected=0, bad_template=0, bad_current=0)

class TrainingLoadTests(unittest.TestCase):

    def start(self):
        s = M.Schedule()
        s.tick(dict(kind='rf_trace_status', faults=0, gain_ready=True), 0)
        self.assertEqual(s.tick(probe(30), 10), ['TRAIN_START'])
        s.tick('PRESENCE_TRAIN_START ms=1', 10.1)
        return s

    def test_complete_workload_never_admits_calibration_or_requests_a_label(self):
        s = self.start()
        commands = []
        for n in range(11, 72):
            commands += s.tick(probe(n * 10), float(n))
        self.assertEqual(commands, ['TRAIN_STOP'])
        self.assertEqual(s.tick('PRESENCE_TRAIN_STOP ms=1 status=0 templates=25', 71.1), ['END'])
        self.assertEqual(s.outcome, 'unlabelled_training_load_complete')
        self.assertIsNone(s.pending_label)

    def test_occupancy_or_detector_admission_is_rejected(self):
        for e in (dict(kind='recorded_label', label=1), dict(kind='recorded_motion', label=0, phase='ready')):
            s = M.Schedule()
            self.assertEqual(s.tick(e, 0), ['END'])
            self.assertIsNotNone(s.outcome)

    def test_loss_stack_or_numerical_failure_stops_once(self):
        for e in (dict(kind='lab_recorder_status', losses=1, rf_stack=1024, gain_stack=1024, compute_stack=1024), dict(kind='lab_recorder_status', losses=0, rf_stack=500, gain_stack=1024, compute_stack=1024), dict(kind='numeric_failure')):
            s = self.start()
            self.assertEqual(s.tick(e, 11), ['END'])
            self.assertEqual(s.tick(e, 12), [])

    def test_silence_clock_and_human_input_fail_closed(self):
        s = M.Schedule()
        self.assertEqual(s.tick(None, 10), ['END'])
        self.assertEqual(s.outcome, 'numerical_workload_stalled')
        s = self.start()
        self.assertEqual(s.tick(None, 13), ['END'])
        with self.assertRaises(ValueError):
            M.Schedule().tick(None, float('nan'))
        with self.assertRaises(ValueError):
            M.Schedule().confirm('out', 1)
if __name__ == '__main__':
    unittest.main()
