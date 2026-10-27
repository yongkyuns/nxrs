"""Shared wire-fixture and API security regressions, not a gateway server test."""
import copy
import json
from pathlib import Path
import unittest
from jsonschema import Draft202012Validator, ValidationError

ROOT = Path(__file__).resolve().parents[2]
SPEC = json.loads((ROOT / 'docs/monitoring-api.json').read_text())
FIXTURES = ROOT / 'clients/ios/Tests/NxrsMonitorCoreTests/Fixtures'


def validate(name, value):
    schema = {'$ref': f'#/components/schemas/{name}', 'components': SPEC['components']}
    Draft202012Validator(schema).validate(value)


def response_schema(response):
    if '$ref' in response:
        section, name = response['$ref'].removeprefix('#/components/').split('/')
        return SPEC['components'][section][name]
    return response


class MonitoringContractTests(unittest.TestCase):
    def test_schema_and_shared_fixtures(self):
        for schema in SPEC['components']['schemas'].values():
            Draft202012Validator.check_schema(schema)
        for name, file in [('CameraList', 'cameras.json'), ('PlaybackGrant', 'grant.json'), ('DetectionSnapshot', 'detection.json'), ('ActivityPage', 'activity.json')]:
            validate(name, json.loads((FIXTURES / file).read_text()))

    def test_application_authentication_is_not_optional(self):
        self.assertEqual(SPEC['security'], [{'accountBearer': []}])
        for path, methods in SPEC['paths'].items():
            for operation in methods.values():
                if path == '/healthz':
                    self.assertEqual(operation['security'], [])
                else:
                    self.assertNotIn('security', operation)
                    self.assertIn('401', operation['responses'])
                    self.assertIn('403', operation['responses'])

    def test_decoder_progress_has_bounded_complete_timings_when_present(self):
        body=json.loads((FIXTURES/'activity.json').read_text())
        timing=dict(observer_run=1,recoveries=1,last_recovery_ms=5000,max_recovery_ms=5000,
                    max_frame_gap_ms=5000,frame_gaps_over_30s=0)
        body['health']['progress']=timing
        validate('ActivityPage',body)
        for key,value in [('observer_run',0),('last_recovery_ms',-1),('max_frame_gap_ms',1<<54)]:
            invalid=copy.deepcopy(body); invalid['health']['progress'][key]=value
            with self.subTest(field=key), self.assertRaises(ValidationError):
                validate('ActivityPage',invalid)
        del body['health']['progress']['max_recovery_ms']
        with self.assertRaises(ValidationError):
            validate('ActivityPage',body)
        del body['health']['progress']
        validate('ActivityPage',body)

    def test_success_responses_are_not_cacheable(self):
        for methods in SPEC['paths'].values():
            for operation in methods.values():
                for response in operation['responses'].values():
                    response = response_schema(response)
                    self.assertEqual(response['headers']['Cache-Control']['schema']['const'], 'no-store')

    def test_camera_cannot_omit_synthetic_or_physical_classification(self):
        body = json.loads((FIXTURES / 'cameras.json').read_text())
        self.assertEqual(body['cameras'][0]['source'], 'synthetic')
        del body['cameras'][0]['source']
        with self.assertRaises(ValidationError):
            validate('CameraList', body)

    def test_bad_grant_shape_is_rejected(self):
        original = json.loads((FIXTURES / 'grant.json').read_text())
        for field, value in [('protocol', 'rtsp'), ('token_type', 'Basic'), ('expires_at', 0),
                             ('endpoint', 'http://media.example.test/whep'), ('camera_id', '../admin'),
                             ('access_token', 'token\nHeader: value')]:
            body = copy.deepcopy(original)
            body[field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate('PlaybackGrant', body)

    def test_bounded_catalog(self):
        body = json.loads((FIXTURES / 'cameras.json').read_text())
        body['cameras'] *= 65
        with self.assertRaises(ValidationError):
            validate('CameraList', body)

    def test_detection_arrays_bounds_and_schema_are_shared(self):
        original = json.loads((FIXTURES / 'detection.json').read_text())
        for count in (11, 100):
            body = copy.deepcopy(original)
            body['source']['latest']['objects'] *= count
            with self.assertRaises(ValidationError):
                validate('DetectionSnapshot', body)
        body = copy.deepcopy(original)
        body['source']['latest']['person_score_permille'] = 1001
        with self.assertRaises(ValidationError):
            validate('DetectionSnapshot', body)
        schema = json.loads((ROOT / 'docs/detection-protocol.schema.json').read_text())
        Draft202012Validator(schema).validate(original)
        for name, definition in schema['$defs'].items():
            expected = json.loads(json.dumps(definition).replace('#/$defs/', '#/components/schemas/Detection'))
            self.assertEqual(expected, SPEC['components']['schemas']['Detection' + name])

    def test_in_band_video_contract_reuses_the_same_bounded_source(self):
        frame = json.loads((FIXTURES / 'video-detection.json').read_text())
        validate('DetectionVideoFrame', frame)
        frame['epoch'] = 'not-an-epoch'
        with self.assertRaises(ValidationError): validate('DetectionVideoFrame', frame)

    def test_contract_has_no_network_identity_fields(self):
        schemas = json.dumps(SPEC['components']['schemas']).lower()
        for forbidden in ['wireguard', 'peer_key', 'private_key', 'subnet', 'lan_ip']:
            self.assertNotIn(forbidden, schemas)


if __name__ == '__main__':
    unittest.main()
