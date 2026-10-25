import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
import strict_vpn
import vpn


class StrictDeploymentTests(unittest.TestCase):
    def test_strict_services_preserve_hardening_and_bind_failstop_group(self):
        units = strict_vpn.services()
        for name in ['nxrs-control', 'nxrs-media', 'nxrs-proxy']:
            unit = units[name + '.service']
            for directive in ['NoNewPrivileges=yes', 'ProtectSystem=strict', 'ProtectHome=yes',
                              'PrivateDevices=yes', 'CapabilityBoundingSet=', 'Restart=no',
                              'PartOf=nxrs-monitor.target', 'OnFailure=nxrs-monitor-stop.service', 'Requires=wg-quick@wg-nxrs.service nxrs-firewall.service']:
                self.assertIn(directive, unit)
            self.assertNotIn('WantedBy=multi-user.target', unit)
            self.assertNotIn('ExecStartPre=/usr/bin/python', unit)
        control = units['nxrs-control.service']
        self.assertIn('--security /run/nxrs-control/security.json', control)
        self.assertIn('ExecStartPre=/usr/bin/install -m 0600 %d/security /run/nxrs-control/security.json', control)
        self.assertIn('RuntimeDirectoryMode=0700', control)
        self.assertIn('DynamicUser=no', control)
        self.assertIn('StateDirectory=nxrs-control', control)
        self.assertIn('StateDirectoryMode=0700', control)
        for name in ['nxrs-media', 'nxrs-proxy']:
            self.assertIn('BindsTo=nxrs-control.service', units[name + '.service'])
            self.assertIn('After=nxrs-control.service', units[name + '.service'])
            self.assertIn('DynamicUser=yes', units[name + '.service'])
        self.assertIn('BindsTo=nxrs-control.service nxrs-media.service nxrs-proxy.service', units['nxrs-monitor.target'])
        self.assertIn('WantedBy=multi-user.target', units['nxrs-monitor.target'])

    def test_failure_handler_can_only_queue_an_owned_group_stop(self):
        unit=strict_vpn.services()['nxrs-monitor-stop.service']
        self.assertIn('Type=oneshot\nUser=root\nGroup=root\n',unit)
        starts=[line.removeprefix('ExecStart=') for line in unit.splitlines() if line.startswith('ExecStart=')]
        self.assertEqual(starts,['/usr/bin/systemctl --no-block stop nxrs-control.service nxrs-media.service nxrs-proxy.service nxrs-monitor.target'])
        self.assertNotIn('PartOf=',unit)
        self.assertNotIn('OnFailure=',unit)
        self.assertNotIn('[Install]',unit)
        for setting in ['NoNewPrivileges=yes','ProtectSystem=strict','CapabilityBoundingSet=','RestrictAddressFamilies=AF_UNIX','TimeoutStartSec=5']:
            self.assertIn(setting,unit)

    @unittest.skipUnless(os.name == 'posix' and shutil.which('install'), 'Unix install tool')
    def test_staging_credential_has_private_mode_without_altering_source_or_state(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source, target, state = root/'credential', root/'runtime', root/'revocations.json'
            source.write_bytes(b'test-only credential')
            source.chmod(0o440)  # Mode mask resembling named-user ACL credential access.
            state.write_bytes(b'{"version":1,"revoked":["lost_phone"]}')
            subprocess.run([shutil.which('install'), '-m', '0600', str(source), str(target)], check=True)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(target.stat().st_nlink, 1)
            self.assertEqual(target.stat().st_uid, os.geteuid())
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o440)
            self.assertEqual(json.loads(state.read_text())['revoked'], ['lost_phone'])

    def test_management_is_loopback_only_and_not_exempt_from_auth(self):
        config = strict_vpn.media_config('10.77.0.1')
        self.assertTrue(config['api'])
        self.assertEqual(config['apiAddress'], '127.0.0.1:9997')
        self.assertEqual(config['authHTTPExclude'], [])
        self.assertEqual(config['webrtcAdditionalHosts'], ['10.77.0.1'])
        self.assertNotIn('9997', vpn.nginx_config('camera.test', '10.77.0.1'))
        self.assertNotIn('9997', vpn.firewall('10.77.0.1', '10.77.0.2'))
        self.assertFalse(vpn.media_config('10.77.0.1')['api'])
        self.assertNotIn('--security', vpn.services()['nxrs-control.service'])

    def test_generated_credentials_are_distinct_private_and_no_state_is_reset(self):
        public = base64.b64encode(bytes(range(32))).decode()
        responses = [subprocess.CompletedProcess([], 0, stdout=b'private-test-key\n'),
                     subprocess.CompletedProcess([], 0, stdout=(public+'\n').encode())]
        with tempfile.TemporaryDirectory() as parent, patch.object(vpn.subprocess, 'run', side_effect=responses) as run:
            root = Path(parent) / 'bundle'
            strict_vpn.generate(root, 'camera.test', '10.77.0.1', '10.77.0.2', 'endpoint.test', public)
            self.assertEqual(run.call_count, 2)
            config = json.loads((root/'config.json').read_text())
            security = json.loads((root/'security.json').read_text())
            digest = hashlib.sha256(security['media_api_token'].encode()).hexdigest()
            self.assertNotEqual(digest, config['viewers'][0]['token_sha256'])
            self.assertNotEqual(digest, config['publishers'][0]['token_sha256'])
            self.assertEqual(security['revocations_file'], strict_vpn.STATE)
            self.assertFalse((root/'revocations.json').exists())
            self.assertEqual(json.loads((root/'deployment.json').read_text())['mode'], 'strict')
            if os.name == 'posix':
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                for file in root.iterdir():
                    self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
            original = (root/'security.json').read_bytes()
            with self.assertRaises(FileExistsError):
                strict_vpn.generate(root, 'camera.test', '10.77.0.1', '10.77.0.2', 'endpoint.test', public)
            self.assertEqual((root/'security.json').read_bytes(), original)
            self.assertEqual(run.call_count, 2)

    def test_template_drift_fails_instead_of_silently_disabling_security(self):
        with self.assertRaises(ValueError):
            strict_vpn.replace_once('no match', 'old', 'new')
        with self.assertRaises(ValueError):
            strict_vpn.replace_once('old old', 'old', 'new')


@unittest.skipUnless(os.name == 'posix', 'Unix state provisioning')
class StateInitializationTests(unittest.TestCase):
    def test_explicit_empty_private_seed_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            strict_vpn.initialize_state(root)
            path = root/'revocations.json'
            self.assertEqual(json.loads(path.read_text()), {'version': 1, 'revoked': []})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            path.write_text('{"version":1,"revoked":["lost_phone"]}')
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                strict_vpn.initialize_state(root)
            self.assertEqual(path.read_bytes(), before)

    def test_missing_corrupt_and_leftover_state_are_not_repaired(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with self.assertRaises(OSError):
                strict_vpn.initialize_state(root/'missing')
            for filename in ['revocations.json', '.nxrs-revocations.lock', '.nxrs-initializing', 'backup.json']:
                path = root/filename
                path.write_text('corrupt or previous enrollment')
                with self.assertRaises(FileExistsError):
                    strict_vpn.initialize_state(root)
                self.assertEqual(path.read_text(), 'corrupt or previous enrollment')
                path.unlink()

    def test_symlinks_and_public_directories_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            link = root/'link'
            link.symlink_to(root)
            with self.assertRaises(OSError):
                strict_vpn.initialize_state(link)
            link.unlink()
            root.chmod(0o755)
            with self.assertRaises(ValueError):
                strict_vpn.initialize_state(root)
            self.assertEqual(list(root.iterdir()), [])

    def test_failed_sync_keeps_marker_and_blocks_automatic_retry(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with patch.object(strict_vpn.os, 'fsync', side_effect=OSError('test sync failure')):
                with self.assertRaises(OSError):
                    strict_vpn.initialize_state(root)
            self.assertTrue((root/'.nxrs-initializing').exists())
            with self.assertRaises(FileExistsError):
                strict_vpn.initialize_state(root)


if __name__ == '__main__':
    unittest.main()
