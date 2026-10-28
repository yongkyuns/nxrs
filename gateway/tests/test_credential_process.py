"""A retired process is distinct from an uninspectable live process."""
import sys
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parent))
import credential_process

class ProcessMetadataLifetimeTests(unittest.TestCase):
    def test_process_disappearing_between_proc_files_is_no_longer_observable(self):
        for missing in [FileNotFoundError(),ProcessLookupError()]:
            with mock.patch.object(credential_process.sys,'platform','linux'), \
                 mock.patch.object(credential_process.Path,'read_bytes',side_effect=[b'public-command',missing]):
                self.assertIsNone(credential_process.credential_free(123,['synthetic']))

    def test_permission_failure_is_not_a_retired_process_or_clean_observation(self):
        with mock.patch.object(credential_process.sys,'platform','linux'), \
             mock.patch.object(credential_process.Path,'read_bytes',side_effect=PermissionError()):
            with self.assertRaises(PermissionError):
                credential_process.credential_free(123,['synthetic'])

if __name__=='__main__':unittest.main()
