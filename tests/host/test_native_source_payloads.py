"""Offline source-chain and publication refusals; actual GnuPG belongs to hosted CI."""
import copy
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/native-packages'))
import source_payloads as s


class SourcePayloadTests(unittest.TestCase):
    def case(self):
        return copy.deepcopy(s.CASES['systemd-journald-wall'])

    def record(self, case=None):
        case = case or self.case()
        files = {**case['archives'], case['descriptor']: dict(size=4, sha256='a' * 64)}
        return dict(package=[case['source']], version=[case['version']],
                    directory=[case['directory']], **{'checksums-sha256': ['', *(
                        f"{v['sha256']} {v['size']} {name}" for name, v in files.items())]})

    def index(self, row):
        lines = []
        for key, values in row.items():
            lines.append(key + ': ' + values[0])
            lines.extend(' ' + value for value in values[1:])
        return ('\n'.join(lines) + '\n\n').encode()

    def test_exact_source_and_multiline_checksums_survive_full_index_parsing(self):
        row = self.record(); row['maintainer'] = ['Unicode name é', 'continued non-identity']
        other = dict(package=['other'], version=['1'], directory=['pool/main/o/other'])
        raw = self.index(other) + self.index(row)
        actual = s.select_source(io.BytesIO(raw), self.case())
        self.assertEqual(actual['source'], 'systemd')
        self.assertEqual(actual['version'], '255.4-1ubuntu8.18')
        self.assertEqual(set(actual['files']), set(self.case()['archives']) | {self.case()['descriptor']})

    def test_duplicate_or_malformed_trailing_record_cannot_replace_a_valid_match(self):
        good = self.index(self.record())
        for raw in [good + good, good + b'invalid field\n']:
            with self.subTest(raw=raw[-30:]), self.assertRaises(ValueError):
                s.select_source(io.BytesIO(raw), self.case())

    def test_case_insensitive_duplicates_and_continued_identity_refuse(self):
        for raw in [b'Package: systemd\npACKAGE: other\n',
                    b'Package: systemd\n other\n', b'Version: 1\n 2\n',
                    b'Directory: pool/main/s/systemd\n other\n', b' orphan\n',
                    b'Package: systemd\r\n', b'Package: sys\0temd\n']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                list(s.stanzas(io.BytesIO(raw)))

    def test_wrong_identity_directory_or_reviewed_archive_bytes_refuse(self):
        cases = []
        for key, values in [('package', ['other']), ('version', ['255.4-1ubuntu8.19']),
                            ('directory', ['pool/main/s/../systemd'])]:
            row = self.record(); row[key] = values; cases.append(row)
        row = self.record(); row['checksums-sha256'][1] = row['checksums-sha256'][1].replace(
            self.case()['archives']['systemd_255.4.orig.tar.gz']['sha256'], 'b' * 64)
        cases.append(row)
        for row in cases:
            with self.assertRaises(ValueError): s.source_record(row, self.case())

    def test_source_file_set_basenames_digests_and_bounds_are_mandatory(self):
        base = self.record(); mutations = []
        for value in ['', 'a' * 64 + ' 1 ../escape', 'a' * 64 + ' 1 /escape',
                      'a' * 64 + ' 1 name%2fescape', 'a' * 64 + ' 0 name',
                      'a' * 64 + ' ' + str(s.p.MAX_ARCHIVE + 1) + ' name', 'bad 1 name']:
            row = copy.deepcopy(base); row['checksums-sha256'][1] = value; mutations.append(row)
        row = copy.deepcopy(base); row['checksums-sha256'].append(row['checksums-sha256'][1]); mutations.append(row)
        row = copy.deepcopy(base); row['checksums-sha256'].pop(); mutations.append(row)
        row = copy.deepcopy(base); row['checksums-sha256'][0] = 'folded'; mutations.append(row)
        for row in mutations:
            with self.assertRaises(ValueError): s.source_record(row, self.case())

    def test_unrelated_versions_never_satisfy_exact_review(self):
        row = self.record(); row['version'] = ['255.4-1ubuntu8.19']
        self.assertIsNone(s.select_source(io.BytesIO(self.index(row)), self.case()))

    def test_python_review_selects_only_its_exact_source_record(self):
        python = copy.deepcopy(s.CASES['python312-tls-distrust'])
        raw = self.index(self.record()) + self.index(self.record(python))
        result = s.select_source(io.BytesIO(raw), python)
        self.assertEqual((result['source'], result['version']),
                         ('python3.12', '3.12.3-1ubuntu0.17'))
        self.assertEqual(set(result['files']), set(python['archives']) | {python['descriptor']})
        for field, replacement in [('package', ['systemd']),
                                   ('version', ['3.12.3-1ubuntu0.18']),
                                   ('directory', ['pool/main/s/systemd'])]:
            row = self.record(python); row[field] = replacement
            with self.subTest(field=field), self.assertRaises(ValueError):
                s.source_record(row, python)

    def test_python_review_requires_both_original_and_distribution_patch_archives(self):
        python = copy.deepcopy(s.CASES['python312-tls-distrust'])
        for name in python['archives']:
            row = self.record(python)
            row['checksums-sha256'] = [line for line in row['checksums-sha256']
                                       if not line.endswith(' ' + name)]
            with self.subTest(missing=name), self.assertRaises(ValueError):
                s.source_record(row, python)

    def test_decompressed_index_and_line_budgets_refuse(self):
        with patch.object(s, 'MAX_INDEX', 8), self.assertRaisesRegex(ValueError, 'source-index-budget'):
            list(s.stanzas(io.BytesIO(b'Package: systemd\n')))
        with self.assertRaisesRegex(ValueError, 'source-index-budget'):
            list(s.stanzas(io.BytesIO(b'A: ' + b'x' * 32768 + b'\n')))

    def fixture_case(self):
        case = self.case()
        contents = {name: ('synthetic source fixture ' + name).encode() for name in case['archives']}
        case['archives'] = {name: dict(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
                            for name, raw in contents.items()}
        descriptor = b'desc'; contents[case['descriptor']] = descriptor
        row = self.record(case)
        row['checksums-sha256'][-1] = f"{hashlib.sha256(descriptor).hexdigest()} 4 {case['descriptor']}"
        return case, contents, gzip.compress(self.index(row))

    def authenticate_fixture(self, root, corrupt=False, missing=False):
        case, contents, compressed = self.fixture_case()
        if missing:
            row = self.record(case); row['version'] = ['0']; compressed = gzip.compress(self.index(row))
        identity = dict(size=len(compressed), sha256=hashlib.sha256(compressed).hexdigest())
        calls = []
        class Evidence:
            work = root
            def get(self, url, path, maximum, expected):
                calls.append(path.name)
                raw = compressed if path.name == 'Sources.gz' else contents[path.name]
                if corrupt and path.name != 'Sources.gz': raw += b'changed'
                self_result = dict(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
                s.p.archive_identity(self_result['size'], self_result['sha256'], expected)
                path.write_bytes(raw)
                return self_result
        def release(evidence, trust, suite, now):
            folder = root / suite; folder.mkdir()
            return folder, {'main/source/Sources.gz': identity}, dict(
                suite=suite, release_sha256='b' * 64, bad_signature_refused=True)
        trust = dict(suites=['noble-updates'], archive='https://archive.ubuntu.com/ubuntu/')
        with patch.object(s.p, 'verify_release', side_effect=release):
            result = s.authenticate(Evidence(), trust, case)
        return result, calls

    def test_signed_index_resolution_checks_every_archive_and_actual_mutations(self):
        with tempfile.TemporaryDirectory() as name:
            result, calls = self.authenticate_fixture(Path(name))
        self.assertEqual(len(calls), 4)
        self.assertEqual(result['changed_index_refusals'], 1)
        self.assertEqual(result['changed_archive_refusals'], 3)
        self.assertTrue(all(v['changed_archive_refused'] for v in result['authenticated_files'].values()))

    def test_absent_exact_version_or_changed_download_never_authenticates(self):
        for arguments in [dict(corrupt=True), dict(missing=True)]:
            with tempfile.TemporaryDirectory() as name, self.assertRaises(ValueError):
                self.authenticate_fixture(Path(name), **arguments)

    def test_personal_host_guard_precedes_all_file_tool_and_network_operations(self):
        with tempfile.TemporaryDirectory() as name:
            work = Path(name) / 'work'; report = Path(name) / 'report'
            with patch.object(s.u, 'hosted', side_effect=s.u.Refusal('hosted-ubuntu-required')), \
                 patch.object(s, 'inputs') as inputs, patch.object(s.p, 'Evidence') as evidence:
                with self.assertRaisesRegex(ValueError, 'hosted-ubuntu-required'):
                    s.run('systemd-journald-wall', work, report)
                inputs.assert_not_called(); evidence.assert_not_called()
            self.assertFalse(work.exists()); self.assertFalse(report.exists())

    def run_fixture(self, directory, failure=None, changed=False,
                    case_name='systemd-journald-wall'):
        root = Path(directory); keyring = root / 'keyring'; keyring.write_bytes(b'untrusted fixture bytes')
        work = root / 'work'; report = root / 'report'; original_policy = s.p.policy
        def command(args): return 'a' * 40 if 'rev-parse' in args else ''
        with patch.object(s.u, 'hosted'), patch.object(s.u, 'command', side_effect=command), \
             patch.object(s, 'inputs', side_effect=[{'owned': 'b' * 64}, {'owned': ('c' if changed else 'b') * 64}]), \
             patch.object(s.p, 'KEYRING', keyring), patch.object(s.p, 'Evidence'), \
             patch.object(s.p, 'policy', side_effect=lambda trust, now: original_policy(
                 trust, datetime(2026, 10, 27, tzinfo=timezone.utc))), \
             patch.object(s, 'authenticate', side_effect=failure, return_value=dict(control_fixture_only=True)):
            if failure or changed:
                with self.assertRaises(Exception): s.run(case_name, work, report)
            else: s.run(case_name, work, report)
        return json.loads(report.read_bytes())

    def test_source_authentication_success_cannot_clear_other_acceptance_gates(self):
        with tempfile.TemporaryDirectory() as name: result = self.run_fixture(name)
        self.assertTrue(result['qualified']); self.assertEqual(result['verification_status'], 'complete')
        self.assertTrue(result['signed_source_index_authenticated'])
        self.assertTrue(result['source_payloads_authenticated'])
        for flag in s.FLAGS: self.assertIs(result[flag], False)

    def test_python_source_admission_cannot_claim_installed_tls_mitigation(self):
        with tempfile.TemporaryDirectory() as name:
            result = self.run_fixture(name, case_name='python312-tls-distrust')
        self.assertTrue(result['source_payloads_authenticated'])
        self.assertEqual(result['case'], 'python312-tls-distrust')
        self.assertEqual(result['reviewed_source']['source'], 'python3.12')
        for flag in s.FLAGS: self.assertIs(result[flag], False)

    def test_failure_or_changed_source_never_publishes_success_or_private_error_text(self):
        for arguments in [dict(failure=s.u.Refusal('archive-digest')), dict(changed=True),
                          dict(failure=RuntimeError('/private/fixture-secret is not public'))]:
            with tempfile.TemporaryDirectory() as name: result = self.run_fixture(name, **arguments)
            self.assertFalse(result['qualified']); self.assertEqual(result['verification_status'], 'failed')
            self.assertFalse(result['source_payloads_authenticated'])
            self.assertNotIn('fixture-secret', json.dumps(result))
            for flag in s.FLAGS: self.assertIs(result[flag], False)

    def test_oversized_receipt_refuses_before_publication(self):
        with tempfile.TemporaryDirectory() as name:
            report = Path(name) / 'report'
            with self.assertRaisesRegex(ValueError, 'source-receipt-budget'):
                s.save(report, dict(data='x' * s.MAX_REPORT))
            self.assertFalse(report.exists())


if __name__ == '__main__': unittest.main()
