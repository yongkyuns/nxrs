"""Generation regressions; systemd_reference.py verifies actual write denial."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
import strict_vpn


class StrictRuntimeIsolationTests(unittest.TestCase):
    def test_run_is_explicitly_read_only_in_all_strict_services(self):
        units = strict_vpn.services()
        for name in ['nxrs-control', 'nxrs-media', 'nxrs-proxy']:
            with self.subTest(service=name):
                lines = units[name + '.service'].splitlines()
                self.assertEqual(lines.count('ProtectSystem=strict'), 1)
                self.assertEqual(lines.count('ReadOnlyPaths=/run'), 1)
                self.assertNotIn('ReadWritePaths=/run', lines)
                self.assertIn('NoNewPrivileges=yes', lines)
                self.assertIn('CapabilityBoundingSet=', lines)
        self.assertIn('RuntimeDirectory=nxrs-control\n', units['nxrs-control.service'])
        self.assertIn('StateDirectory=nxrs-control\n', units['nxrs-control.service'])
        self.assertIn('RuntimeDirectory=nxrs-proxy\n', units['nxrs-proxy.service'])

    def test_runtime_probe_is_not_removed_or_weakened(self):
        root = Path(__file__).resolve().parent
        source = (root / 'sandbox_probe.py').read_text()
        self.assertIn("(outside/'forbidden').write_text", source)
        self.assertIn("checks['outside_write_denied'] = True", source)
        self.assertIn('if not all(checks.values()):', source)
        fixture = (root / 'systemd_reference.py').read_text()
        self.assertIn('ExecStartPre=/usr/bin/python3 {probe}', fixture)


if __name__ == '__main__':
    unittest.main()
