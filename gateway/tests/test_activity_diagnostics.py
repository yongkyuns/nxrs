import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent))
from activity_diagnostics import public_failure, worker_stages, snapshot


class ActivityDiagnosticsTests(unittest.TestCase):
    def test_only_fixed_failure_phase_and_bounded_health_leave_observer(self):
        secret='synthetic-private-value'
        raw=('PASS: real RTSP authorization rejects wrong and cross-role recorder credentials\n'
             'ACTIVITY_HEALTH '+json.dumps(dict(frames_observed=25,reconnects=5,clips_created=0,clips_dropped=1,
                                                state=secret,token=secret))+'\n'
             'ACTIVITY_WORKER_STAGES '+json.dumps(['reader exited',secret,'relay stopped'])+'\n'
             'AssertionError: confirmed movement produces an automatic clip\n').encode()
        value=public_failure(raw)
        self.assertEqual(value['phase'],'recorder-roles');self.assertEqual(value['reason'],'motion-clip')
        self.assertEqual(value['health'],dict(frames_observed=25,reconnects=5,clips_created=0,clips_dropped=1))
        self.assertEqual(value['worker_stages'],['reader exited','relay stopped'])
        self.assertTrue(secret not in json.dumps(value),'private value must remain withheld')

    def test_unknown_failure_and_injected_fields_cannot_export_private_text(self):
        secret='synthetic-private-value'
        for raw in [('AssertionError: '+secret).encode(),('PASS: '+secret).encode(),
                    ('ACTIVITY_HEALTH '+json.dumps(dict(frames_observed=secret))).encode(),
                    ('ACTIVITY_WORKER_STAGES '+json.dumps([secret])).encode(),b'ACTIVITY_HEALTH null',
                    b'ACTIVITY_WORKER_STAGES {}',b'ACTIVITY_HEALTH {invalid']:
            value=public_failure(raw)
            self.assertEqual(value['phase'],'unknown');self.assertEqual(value['reason'],'unknown')
            self.assertTrue(secret not in json.dumps(value),'private exception or metadata must remain withheld')

    def test_counter_types_and_magnitude_are_bounded(self):
        for counter in [True,-1,2**63,'private-value',None,1.5]:
            raw=('ACTIVITY_HEALTH '+json.dumps(dict(frames_observed=counter))).encode()
            self.assertEqual(public_failure(raw)['health'],{})

    def test_worker_lines_must_exactly_match_public_categories(self):
        raw=b'nxrs-camera-activity: reader exited\nnxrs-camera-activity: private-value\nreader exited\n'
        self.assertEqual(worker_stages(raw),['reader exited'])
        self.assertEqual(len(worker_stages(b'nxrs-camera-activity: reader exited\n'*30)),8)

    def test_snapshot_projects_only_bounded_health_and_known_terminal_worker_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);health=root/'health';log=root/'worker'
            health.write_text(json.dumps(dict(frames_observed=27,reconnects=5,token='private-value')))
            log.write_text('private-value\nnxrs-camera-activity: activity storage busy\n')
            value=snapshot(health,[log,root/'missing'])
            self.assertEqual(value,dict(health=dict(frames_observed=27,reconnects=5),
                                        worker_stages=['activity storage busy']))
            self.assertNotIn('private-value',json.dumps(value))
            health.write_bytes(b'x'*2049)
            self.assertEqual(snapshot(health,[log])['health'],{})
            health.write_bytes(b'null')
            self.assertEqual(snapshot(health,[log])['health'],{})

    def test_oversized_observations_and_raw_payload_are_not_exported(self):
        raw=b'ACTIVITY_HEALTH '+json.dumps(dict(frames_observed=1,payload='x'*4096)).encode()
        self.assertEqual(public_failure(raw)['health'],{})
        self.assertEqual(public_failure(b'private-frame'*10000)['reason'],'unknown')


if __name__=='__main__':unittest.main()
