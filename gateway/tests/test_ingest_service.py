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
                     '--require-media-isolation', 'ExecStartPre=/usr/local/libexec/nxrs-media-sandbox --check',
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
        with tempfile.TemporaryDirectory() as parent, mock.patch.object(strict_vpn.vpn, 'generate', side_effect=base):
            directory = Path(parent)/'new'
            strict_vpn.generate(directory, 'name', 'a', 'b', 'e', 'k', camera='192.168.1.2:8080',
                                allow_insecure_camera_http=True, source_kind='physical')
            self.assertTrue(json.loads((directory/'deployment.json').read_text())['includes_ingest'])
            self.assertEqual(json.loads((directory/'config.json').read_text())['cameras'][0]['source'], 'physical')
            self.assertTrue((directory/'nxrs-ingest.service').is_file())
            self.assertEqual((directory/'ingest.json').stat().st_mode & 0o777, 0o600)
            self.assertFalse((directory/'revocations.json').exists())

    def test_template_drift_is_not_silently_accepted(self):
        with mock.patch.object(ingest_service.vpn, 'services', return_value={'nxrs-control.service':'changed'}):
            with self.assertRaises(ValueError):
                ingest_service.service()


if __name__ == '__main__':
    unittest.main()
