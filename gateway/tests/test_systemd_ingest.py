"""Unprivileged fixture import/guard regressions: never create a unit or invoke systemctl."""
from pathlib import Path
import unittest
from unittest import mock
import systemd_ingest


class ManagedFixtureTests(unittest.TestCase):
    def test_read_only_process_descriptors_do_not_shadow_unit_properties(self):
        instance = systemd_ingest.ManagedIngest.__new__(systemd_ingest.ManagedIngest)
        with mock.patch.object(instance, 'property', side_effect=lambda key: {'MainPID':'123', 'ExecMainStatus':'0'}[key]):
            self.assertEqual(instance.pid, 123)
            self.assertEqual(instance.returncode, 0)
            with self.assertRaises(AttributeError):
                instance.pid = 42

    def test_non_hosted_constructor_refuses_before_files_or_commands(self):
        with mock.patch.dict(systemd_ingest.os.environ, {'GITHUB_ACTIONS':'false', 'RUNNER_ENVIRONMENT':'self-hosted'}), \
                mock.patch.object(systemd_ingest, 'command') as command, \
                mock.patch.object(systemd_ingest.shutil, 'copyfile') as copy:
            with self.assertRaises(AssertionError):
                systemd_ingest.ManagedIngest(Path('/run/not-created'), Path('/not-a-binary'))
            command.assert_not_called()
            copy.assert_not_called()


if __name__ == '__main__':
    unittest.main()
