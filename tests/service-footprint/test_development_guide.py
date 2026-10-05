"""Keep the service-footprint developer walkthrough tied to the real CLI."""

from pathlib import Path
import re
import subprocess
import sys
import unittest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
GUIDE = HERE / 'DEVELOPMENT.md'


class DevelopmentGuideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = GUIDE.read_text()

    def test_local_markdown_links_resolve(self):
        links = re.findall(r'\[[^]]+\]\(([^)]+)\)', self.text)
        local_links = [link for link in links if not re.match(r'^[a-z]+://', link)]
        self.assertGreaterEqual(len(local_links), 8)
        for link in local_links:
            with self.subTest(link=link):
                self.assertTrue((GUIDE.parent / link.split('#')[0]).resolve().is_file())

    def test_documented_features_exist_in_manifest(self):
        manifest = (HERE / 'Cargo.toml').read_text()
        for feature in ('ffi-scale-entry', 'native-scale-worker', 'borrowed-mq-io',
                        'shared-mq-code', 'packet-inplace-samples', 'packet-service'):
            with self.subTest(feature=feature):
                self.assertRegex(manifest, rf'(?m)^{re.escape(feature)}\s*=')

    def test_documented_runner_commands_are_supported(self):
        for runner, flags in (
            ('run_transport_matrix.py', ('--build', '--case', '--tree', '--sysroot',
                                         '--baseline-config', '--out-root')),
            ('run_transport_paired.py', ('--case', '--tree', '--sysroot', '--out-root')),
        ):
            result = subprocess.run(
                [sys.executable, str(HERE / runner), '--help'],
                cwd=ROOT, check=True, capture_output=True, text=True)
            for flag in flags:
                with self.subTest(runner=runner, flag=flag):
                    self.assertIn(flag, result.stdout)
            self.assertIn(f'tests/service-footprint/{runner}', self.text)
            cases = (('c-packet-wire-2', 'rust-packet-wire-2', 'c-packet-2',
                      'rust-packet-2') if runner == 'run_transport_matrix.py'
                     else ('wire', 'packet', 'packet-inplace'))
            for case in cases:
                with self.subTest(runner=runner, case=case):
                    self.assertIn(case, result.stdout)
                    self.assertIn(f'--case {case}', self.text)

    def test_single_rust_relink_entrypoint_is_documented(self):
        result = subprocess.run(
            [sys.executable, str(HERE / 'relink_rust.py'), '--help'],
            cwd=ROOT, check=True, capture_output=True, text=True)
        for flag in ('--tree', '--sysroot', '--out', '--feature'):
            with self.subTest(flag=flag):
                self.assertIn(flag, result.stdout)
        self.assertIn('relink_rust.py', self.text)
        for variant in ('std-runtime-run1', 'native-runtime-run1'):
            self.assertIn(variant, self.text)


if __name__ == '__main__':
    unittest.main()
