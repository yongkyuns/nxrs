"""Configuration admission; actual encrypted storage/media is tested on Linux."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'deploy'))
import recording_service as recording
import strict_vpn

VOLUME = 'f4e2ec8c-e175-433a-b67e-95cecf256b30'


class RecordingTests(unittest.TestCase):
    def test_invalid_volume_refuses_before_keys_or_output(self):
        with tempfile.TemporaryDirectory() as parent, patch.object(strict_vpn.vpn, 'generate') as generate:
            output = Path(parent)/'bundle'
            for value in ['', VOLUME.upper(), '0'*32, VOLUME+'\n', '../disk', 42]:
                with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                    strict_vpn.generate(output, 'host', 'gateway', 'peer', 'endpoint', 'key', recording_volume=value)
                self.assertFalse(output.exists())
            generate.assert_not_called()

    def test_opt_in_uses_only_encrypted_paths_and_existing_native_recording(self):
        unit = strict_vpn.services()['nxrs-media.service']
        original = strict_vpn.media_config('10.77.0.1')
        self.assertNotIn('record', original['paths']['reference'])
        files = recording.files(VOLUME, unit, json.dumps(original))
        media = json.loads(files['mediamtx.json'])
        self.assertTrue(media['paths']['reference']['record'])
        self.assertEqual(media['paths']['reference']['recordPath'], recording.ROOT+'/media/%path/%Y-%m-%d_%H-%M-%S-%f')
        self.assertFalse(media['playback'])
        self.assertEqual(media['authHTTPExclude'], [])
        self.assertIn('BindsTo='+recording.CHECK, files['nxrs-media.service'])
        self.assertIn('ExecStart=/usr/local/libexec/nxrs-mediamtx-recording %d/config\n', files['nxrs-media.service'])
        self.assertNotIn('DynamicUser=yes', files['nxrs-media.service'])
        self.assertIn('headless', files['nxrs-recordings.crypttab'])
        self.assertIn('After='+recording.MOUNT, files[recording.CHECK])
        self.assertNotIn('luksFormat', files['nxrs-recording-volume'])
        backup, restore = files['nxrs-backup.service'], files['nxrs-restore@.service']
        for operation in [backup, restore]:
            self.assertIn('Conflicts=nxrs-media.service', operation)
            self.assertIn('LoadCredential=password:/run/nxrs-keys/backup.password', operation)
            self.assertIn('Environment=TMPDIR='+recording.ROOT+'/cache/tmp', operation)
            self.assertIn('ReadOnlyPaths='+recording.ROOT+'/media', operation)
        self.assertIn('backup --one-file-system '+recording.ROOT+'/media', backup)
        self.assertIn('/usr/bin/mkdir -m 0700 '+recording.ROOT+'/restore/%i', restore)
        self.assertIn('restore %i:'+recording.ROOT+'/media --verify', restore)
        self.assertFalse(any(name.endswith('.timer') for name in files))

    def test_template_drift_is_not_a_partial_security_profile(self):
        with self.assertRaises(ValueError):
            recording.files(VOLUME, '', '{}')
