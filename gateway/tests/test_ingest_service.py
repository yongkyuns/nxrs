import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'deploy'))
import ingest_service
import strict_vpn


class IngestServiceTests(unittest.TestCase):
    def enrollment(self):
        return dict(site_id='site', camera_id='cam_reference', gateway_id='gateway',
                    issuer_sha256='a'*64, enrollment_generation=1)

    def test_existing_profile_is_unchanged_unless_opted_in(self):
        self.assertNotIn('nxrs-ingest.service', strict_vpn.services())
        units = strict_vpn.services(include_ingest=True)
        self.assertIn('Wants=nxrs-ingest.service', units['nxrs-monitor.target'])
        self.assertNotIn('nxrs-ingest', units['nxrs-monitor.target'].split('BindsTo=')[1].splitlines()[0])

    def test_encoder_group_and_private_credentials(self):
        text = ingest_service.service()
        for rule in ['DynamicUser=yes', 'User=nxrs-ingest', 'KillMode=control-group', 'SendSIGKILL=yes',
                     'Restart=no', 'LimitCORE=0', 'MemoryMax=256M', 'TasksMax=128',
                     'NoNewPrivileges=yes', 'CapabilityBoundingSet=\n', 'ReadOnlyPaths=/run',
                     'RuntimeDirectoryMode=0700', 'BindsTo=nxrs-control.service nxrs-media.service']:
            self.assertIn(rule, text)
        self.assertEqual(text.count('ExecStartPre=/usr/bin/install -m 0600'), 2)
        for forbidden in ['AF_NETLINK', 'security.json', 'revocations', 'StateDirectory=', '/bin/sh']:
            self.assertNotIn(forbidden, text)

    def test_endpoint_and_acknowledgement_are_explicit(self):
        for source in ['8.8.8.8:80', '0.0.0.0:80', '169.254.1.2:80', 'localhost:80',
                       'http://192.168.1.2/stream', '192.168.1.2:0', '127.0.0.1:8554']:
            with self.subTest(source=source), self.assertRaises(ValueError):
                ingest_service.configuration(source, acknowledge_http=True)
        with self.assertRaises(ValueError):
            ingest_service.configuration('192.168.1.2:8080')
        for width, height in [(641, 480), (0, 480), (1922, 480), (640, 1082), (True, 480)]:
            with self.assertRaises(ValueError):
                ingest_service.configuration('192.168.1.2:8080', width, height, True)

    def test_reuses_scoped_publisher_not_viewer_or_management(self):
        config = ingest_service.configuration('192.168.1.2:8080', 640, 480, True)
        self.assertEqual(config['publisher_user'], 'publisher_reference')
        self.assertEqual(config['publisher_token_file'], '/run/nxrs-ingest/publisher.token')
        self.assertEqual(config['rtsp'], '127.0.0.1:8554')
        self.assertNotIn('viewer', json.dumps(config))
        self.assertNotIn('management', json.dumps(config))

    def test_invalid_camera_fails_before_generating_any_files(self):
        with mock.patch.object(strict_vpn.vpn, 'generate') as base:
            with self.assertRaises(ValueError):
                strict_vpn.generate(Path('/not-created'), 'name', 'a', 'b', 'e', 'k', camera='8.8.8.8:80',
                                    allow_insecure_camera_http=True)
            base.assert_not_called()

    def test_new_bundle_contains_opt_in_service_and_explicit_classification(self):
        def base(directory, *args):
            directory.mkdir(mode=0o700)
            (directory/'config.json').write_text(json.dumps({'cameras':[{'source':'synthetic', 'name':'fixture'}]}))
            (directory/'config.json').chmod(0o600)
        for tls in [False, True]:
            with self.subTest(tls=tls), tempfile.TemporaryDirectory() as parent, \
                    mock.patch.object(strict_vpn.vpn, 'generate', side_effect=base):
                directory = Path(parent)/'new'
                strict_vpn.generate(directory, 'name', 'a', 'b', 'e', 'k', camera='192.168.1.2:8080',
                                    allow_insecure_camera_http=not tls, source_kind='physical',
                                    camera_tls=self.enrollment() if tls else None)
                self.assertTrue(json.loads((directory/'deployment.json').read_text())['includes_ingest'])
                self.assertEqual(json.loads((directory/'config.json').read_text())['cameras'][0]['source'], 'physical')
                unit = (directory/'nxrs-ingest.service').read_text()
                self.assertEqual('--require-camera-tls' in unit, tls)
                self.assertEqual(json.loads((directory/'ingest.json').read_text())['allow_insecure_camera_http'], not tls)
                self.assertEqual((directory/'ingest.json').stat().st_mode & 0o777, 0o600)
                self.assertFalse((directory/'revocations.json').exists())

    def test_template_drift_is_not_silently_accepted(self):
        with mock.patch.object(ingest_service.vpn, 'services', return_value={'nxrs-control.service':'changed'}):
            with self.assertRaises(ValueError):
                ingest_service.service()

    def test_tls_bundle_requires_transport_and_matching_catalog_before_writes(self):
        for enrollment in [None, {}, {**self.enrollment(), 'camera_id': 'other'},
                           {**self.enrollment(), 'enrollment_generation': True},
                           {**self.enrollment(), 'issuer_sha256': 'x'*64},
                           {**self.enrollment(), 'client_key_file': '/unexpected'}]:
            with self.subTest(enrollment=enrollment), mock.patch.object(strict_vpn.vpn, 'generate') as base:
                with self.assertRaises(ValueError):
                    strict_vpn.generate(Path('/not-created'), 'name', 'a', 'b', 'e', 'k',
                                        camera='192.168.1.2:443', camera_tls=enrollment)
                base.assert_not_called()
        for options in [dict(camera='192.168.1.2:443', allow_insecure_camera_http=True), {}]:
            with mock.patch.object(strict_vpn.vpn, 'generate') as base, self.assertRaises(ValueError):
                strict_vpn.generate(Path('/not-created'), 'name', 'a', 'b', 'e', 'k',
                                    camera_tls=self.enrollment(), **options)
            base.assert_not_called()

    def test_tls_service_stages_only_gateway_credentials_and_checks_before_start(self):
        config = ingest_service.configuration('192.168.1.2:443', camera_tls=self.enrollment(), detections=True)
        self.assertFalse(config['allow_insecure_camera_http'])
        self.assertEqual(config['detections']['camera_id'], config['camera_tls']['camera_id'])
        unit = strict_vpn.services(include_ingest=True, camera_tls=True)['nxrs-ingest.service']
        command = '/usr/local/libexec/nxrs-camera-ingest /run/nxrs-ingest/config.json --require-camera-tls'
        self.assertIn('ExecStart='+command+'\n', unit)
        self.assertIn('ExecStartPre='+command+' --check-config\n', unit)
        self.assertEqual(unit.count('ExecStartPre=/usr/bin/install -m 0600'), 7)
        for field, name in ingest_service.TLS_FILES.items():
            self.assertEqual(config['camera_tls'][field], '/run/nxrs-ingest/'+name)
            self.assertIn(f'LoadCredential={name}:/etc/nxrs-monitor/{name}\n', unit)
        self.assertNotIn('camera-ca.key', unit)
        self.assertNotIn('--require-camera-tls', ingest_service.service())


if __name__ == '__main__':
    unittest.main()
