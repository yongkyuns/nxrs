"""Reject incomplete, failed or inconsistent target TLS execution receipts."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('target_tls_oracle_test', ROOT / 'tools/device-identity/qualify_tls_nuttx.py')
TOOLS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOLS)


def valid():
    return '\n'.join(['NXRS_TLS_TARGET_ENTERED',
        'NXRS_TLS_RECONNECTED 1 refused_frames=0',
        'NXRS_TLS_RECONNECTED 2 refused_frames=0',
        'NXRS_TLS_TARGET_REPORT captured=200 busy=50 frames=9 tx=3194 rx=3590 gap_ms=70 elapsed_ms=7400 '
        'buffer_bytes=8192 worker_stack=65536 rejected=2 refused_frames=0 accepted=3 disconnected=3 errors=2 retired=1 lease_released=1'])


class TargetTlsOracleTests(unittest.TestCase):
    def test_positive_and_every_required_counter_failure_refuse(self):
        text = valid()
        self.assertEqual(TOOLS.transcript(text, 'NXRS_STATUS_0', 'pass')['rejected'], 2)
        for field, value in dict(captured=0, frames=1, tx=0, rx=0, elapsed_ms=150000,
            buffer_bytes=8193, worker_stack=0, rejected=1, refused_frames=1,
            accepted=2, disconnected=2, errors=0, retired=0, lease_released=0).items():
            import re
            changed = re.sub(rf'\b{field}=[0-9]+', f'{field}={value}', text)
            with self.subTest(field=field), self.assertRaises(ValueError):
                TOOLS.transcript(changed, 'NXRS_STATUS_0', 'pass')

    def test_missing_duplicate_malformed_and_failed_execution_refuse(self):
        text = valid()
        for line in text.splitlines():
            for changed in (text.replace(line, ''), text + '\n' + line):
                with self.assertRaises(ValueError): TOOLS.transcript(changed, 'NXRS_STATUS_0', 'pass')
        for changed in (text + '\nPANIC', text + '\npanicked at', text.replace('tx=3194', 'tx=-1'),
            text.replace('tx=3194', 'tx=9999999999999'), text.replace('tx=3194', 'unknown=3194'),
            text.replace('tx=3194', 'tx=1 tx=2'), text.replace('retired=1', '')):
            with self.assertRaises(ValueError): TOOLS.transcript(changed, 'NXRS_STATUS_0', 'pass')
        for status in ('', 'NXRS_STATUS_1', 'NXRS_STATUS_0\nNXRS_STATUS_0'):
            with self.assertRaises(ValueError): TOOLS.transcript(text, status, 'pass')
        with self.assertRaises(ValueError): TOOLS.transcript(text, 'NXRS_STATUS_0', 'fail')

    def test_source_manifest_names_exist(self):
        self.assertEqual(len(TOOLS.INPUTS), len(set(TOOLS.INPUTS)))
        self.assertTrue(all((ROOT / name).is_file() for name in TOOLS.INPUTS))


if __name__ == '__main__': unittest.main()
