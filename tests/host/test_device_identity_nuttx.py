"""Reject forged/incomplete target transcripts without fabricating SDK execution."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('identity_nuttx_tools', ROOT / 'tools/device-identity/qualify_nuttx.py')
TOOLS = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(TOOLS)


def valid():
    lines = ['NXRS_IDENTITY_TARGET_ENTERED']
    for label in ('A', 'B'):
        lines.extend(['NXRS_IDENTITY_CSR_BEGIN ' + label,
                      '-----BEGIN CERTIFICATE REQUEST-----', 'cHVibGlj',
                      '-----END CERTIFICATE REQUEST-----', 'NXRS_IDENTITY_CSR_END ' + label,
                      'NXRS_IDENTITY_CERTIFICATE_READY ' + label,
                      'NXRS_IDENTITY_CERTIFICATE_MEASURE ' + label + ' bind_ms=200 short_bind_ms=210 expiry_wait_ms=9900',
                      'NXRS_IDENTITY_CERTIFICATE_BOUND ' + label + ' refusals=13 expiry_checked=1'])
    return '\n'.join([*lines, 'NXRS_IDENTITY_TARGET_REPORT ' + TOOLS.REPORT + '768'])


class TargetOracleTests(unittest.TestCase):
    def test_transcript_controls_require_every_execution_marker_and_real_nsh_success(self):
        text = valid()
        self.assertEqual(TOOLS.transcript(text, 'NXRS_STATUS_0', 'pass')['entropy_fixture_bytes_consumed'], 768)
        mutations = [text.replace('NXRS_IDENTITY_TARGET_ENTERED', ''), text + '\nNXRS_IDENTITY_TARGET_ENTERED',
                     text.replace('busy_refused=1', 'busy_refused=0'), text.replace('768', '4097'),
                     text.replace('768', '0'), text.replace('certificates_bound=2', 'certificates_bound=1'),
                     text.replace('certificate_refusals=26', 'certificate_refusals=25'),
                     text.replace('expired_bindings=2', 'expired_bindings=0'),
                     text.replace('short_bind_ms=210', 'short_bind_ms=10001'),
                     text.replace('expiry_wait_ms=9900', 'expiry_wait_ms=0'),
                     text.replace('NXRS_IDENTITY_CERTIFICATE_MEASURE A', ''),
                     text.replace('NXRS_IDENTITY_CERTIFICATE_READY A', ''),
                     text.replace('NXRS_IDENTITY_CERTIFICATE_BOUND B', ''), text + '\nNXRS_IDENTITY_TARGET_REPORT ' + TOOLS.REPORT + '768',
                     text.replace('NXRS_IDENTITY_CSR_BEGIN A', 'NXRS_IDENTITY_CSR_BEGIN B'),
                     text.replace('NXRS_IDENTITY_CSR_END B', ''), text + '\nPANIC',
                     text.replace('-----END CERTIFICATE REQUEST-----', 'wrong'),
                     text + '\nNXRS_IDENTITY_CSR_BEGIN A', text + '\nNXRS_IDENTITY_CSR_END A']
        for value in mutations:
            with self.subTest(mutation=value[-80:]), self.assertRaises(ValueError):
                TOOLS.transcript(value, 'NXRS_STATUS_0', 'pass')
        for status in ('', 'NXRS_STATUS_1', 'NXRS_STATUS_0\nNXRS_STATUS_0'):
            with self.assertRaises(ValueError): TOOLS.transcript(text, status, 'pass')
        with self.assertRaises(ValueError): TOOLS.transcript(text, 'NXRS_STATUS_0', 'fail')

    def test_cargo_json_failures_do_not_publish_paths_command_lines_or_other_messages(self):
        error = {'reason': 'compiler-message', 'message': {'level': 'error', 'code': {'code': 'E0277'},
                 'message': 'linking with /private/compiler failed',
                 'children': [{'message': "undefined reference to `psa_generate_key'\nprivate credential=secret\nrelocation truncated to fit: R_XTENSA_SLOT0_OP\ncannot find -lmissing_sdk"}]}}
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'private.jsonl'
            path.write_text('not-json\n' + json.dumps(error) + '\n' + json.dumps({'reason': 'compiler-artifact', 'message': 'private'}) + '\n')
            with contextlib.redirect_stderr(output): TOOLS.cargo_diagnostics(path)
        self.assertEqual(output.getvalue().splitlines(), [
            'NXRS_IDENTITY_TARGET_RUST_ERROR code=E0277',
            'NXRS_IDENTITY_TARGET_RUST_FAILURE category=link',
            'NXRS_IDENTITY_TARGET_LINK_FAILURE category=relocation-range token=R_XTENSA_SLOT0_OP',
            'NXRS_IDENTITY_TARGET_UNDEFINED symbol=psa_generate_key',
            'NXRS_IDENTITY_TARGET_LINK_FAILURE category=missing-library token=missing_sdk'])

    @unittest.skipUnless(shutil.which('cargo'), 'Cargo required for real configuration refusal')
    def test_actual_cargo_configuration_refusal_is_classified_without_its_private_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'Cargo.toml').write_text('[package]\nname="private-cargo-control"\nversion="0.1.0"\nedition="2021"\n[workspace]\n')
            (root / 'src').mkdir(); (root / 'src/main.rs').write_text('fn main() {}\n')
            env = dict(os.environ, CARGO_TERM_COLOR='never')
            result = subprocess.run(['cargo', '+1.90.0', 'check', '--offline', '--locked'],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            output = io.StringIO()
            with contextlib.redirect_stderr(output): TOOLS.diagnostics(result.stderr)
            self.assertEqual(output.getvalue().splitlines(), ['NXRS_IDENTITY_TARGET_CARGO_FAILURE category=locked-resolution'])
            self.assertNotIn(str(root), output.getvalue())

    @unittest.skipUnless(shutil.which('cargo'), 'Cargo required for actual compiler diagnostics')
    def test_actual_structured_compiler_error_is_classified_without_source_or_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'Cargo.toml').write_text('[package]\nname="private-compiler-control"\nversion="0.1.0"\nedition="2021"\n[workspace]\n')
            (root / 'src').mkdir(); (root / 'src/main.rs').write_text('fn main() { missing_private_symbol(); }\n')
            env = dict(os.environ, CARGO_TERM_COLOR='never')
            result = subprocess.run(['cargo', '+1.90.0', 'check', '--offline', '--message-format=json'],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            path = root / 'private.jsonl'; path.write_text(result.stdout)
            output = io.StringIO()
            with contextlib.redirect_stderr(output): TOOLS.cargo_diagnostics(path)
            self.assertEqual(output.getvalue().splitlines(), ['NXRS_IDENTITY_TARGET_RUST_ERROR code=E0425'])
            self.assertNotIn(str(root), output.getvalue())
            self.assertNotIn('missing_private_symbol', output.getvalue())

    def test_actual_shell_array_refusal_reports_only_known_source_variable(self):
        result = subprocess.run(['bash', '-euc', 'unset CARGO_CONFIG_ARGS; : "${CARGO_CONFIG_ARGS[0]}"'],
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        output = io.StringIO()
        with contextlib.redirect_stderr(output): TOOLS.diagnostics(result.stderr)
        self.assertEqual(output.getvalue().splitlines(), [
            'NXRS_IDENTITY_TARGET_SHELL_FAILURE category=unbound-variable',
            'NXRS_IDENTITY_TARGET_SHELL_VARIABLE token=CARGO_CONFIG_ARGS'])

    def test_native_stdout_categories_preserve_no_capture_source_paths_or_linker_text(self):
        raw = "NXRS_NATIVE_INPUT_FAILURE category=dependency-probe source=private:/tmp/credential\nregion `iram0_0_seg' overflowed by 23000 bytes\nprivate link command and credential-control\n"
        output = io.StringIO()
        with contextlib.redirect_stderr(output): TOOLS.diagnostics(raw)
        self.assertEqual(output.getvalue().splitlines(), [
            'NXRS_IDENTITY_TARGET_CAPTURE_FAILURE category=dependency-probe',
            'NXRS_IDENTITY_TARGET_LINK_MEMORY region=iram0_0_seg overflow_bytes=23000'])

    def test_private_diagnostics_publish_only_allowlisted_identifiers(self):
        raw = "NXRS_NUTTX_BUILD_STAGE=identity-sdk\nprivate /tmp/secret HOME=value\nundefined reference to `psa_generate_key'\nerror[E0277]: /private/source\nNXRS_IDENTITY_SDK_FAILURE stage=helper category=missing-header token=stddef.h\n"
        output = io.StringIO()
        with contextlib.redirect_stderr(output): TOOLS.diagnostics(raw)
        self.assertEqual(output.getvalue().splitlines(), [
            'NXRS_IDENTITY_TARGET_FAILURE stage=identity-sdk',
            'NXRS_IDENTITY_TARGET_UNDEFINED symbol=psa_generate_key',
            'NXRS_IDENTITY_TARGET_RUST_ERROR code=E0277',
            'NXRS_IDENTITY_SDK_FAILURE stage=helper category=missing-header token=stddef.h'])


if __name__ == '__main__': unittest.main()
