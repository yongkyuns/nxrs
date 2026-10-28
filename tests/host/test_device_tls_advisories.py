"""Advisory freshness, exact source/profile binding and nonempty coverage controls."""
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('device_advisories', ROOT / 'tools/device-identity/advisories.py')
audit = importlib.util.module_from_spec(spec); spec.loader.exec_module(audit)


class AdvisoryTests(unittest.TestCase):
    def setUp(self):
        self.catalogue = json.loads((ROOT / 'security/device-tls-advisory-review.json').read_bytes())
        self.now = datetime.fromisoformat(self.catalogue['reviewed_at']) + timedelta(hours=12)
        self.captured = dict(checked_at=self.now.isoformat(), records=[{k:r[k] for k in ('id','url','sha256')} for r in self.catalogue['advisories']])
        self.inputs = self.catalogue['policy_inputs'].copy()
        self.features = self.catalogue['resolved_features'].copy()
        self.sdk = self.catalogue['sdk'].copy()

    def review(self):
        return audit.review(self.captured, self.catalogue, self.sdk, self.inputs, self.features, self.now)

    def test_reviewed_policy_pins_match_the_actual_checked_in_crypto_recipe(self):
        import hashlib
        for name,expected in self.catalogue['policy_inputs'].items():
            with self.subTest(source=name):
                self.assertEqual(hashlib.sha256((ROOT/name).read_bytes()).hexdigest(),expected)

    def test_complete_exact_review_covers_every_current_article_without_claiming_product_closure(self):
        result = self.review()
        self.assertEqual(result['advisory_count'], len(self.captured['records']))
        self.assertGreaterEqual(result['advisory_count'], 81)
        decisions = {r['id']:r['assessment'] for r in result['reviewed_advisories']}
        self.assertEqual(decisions['mbedtls-security-advisory-2026-07-tls13-hrr-unadvertised-group'], 'excluded-by-resolved-profile')
        self.assertEqual(decisions['mbedtls-security-advisory-2026-07-signature-algorithm-restrictions-certificate-chain'], 'explicit-certificate-profile')
        self.assertNotIn('native_advisory_closure', result)

    def test_new_missing_changed_duplicate_empty_and_wrong_origin_records_refuse(self):
        original = copy.deepcopy(self.captured['records'])
        cases = [[], original[:-1], original + [original[0]], original + [dict(original[0],id='new-advisory')],
                 [dict(original[0],sha256='0'*64),*original[1:]], [dict(original[0],url='https://untrusted.invalid/'),*original[1:]]]
        for records in cases:
            with self.subTest(count=len(records)):
                self.captured['records'] = records
                with self.assertRaises(ValueError): self.review()

    def test_affected_release_or_bundled_crypto_mismatch_and_rust_policy_changes_refuse(self):
        original = self.sdk.copy()
        for key,value in [('version','4.1.0'),('bundled_tf_psa_crypto_version','1.1.0'),('sha256','0'*64)]:
            self.sdk = dict(original,**{key:value})
            with self.subTest(key=key), self.assertRaises(ValueError): self.review()
        self.sdk = original
        self.inputs['driver/device-identity/src/certificate/revocation/tls.rs'] = '0'*64
        with self.assertRaises(ValueError): self.review()

    def test_stale_future_expired_unbounded_and_naive_reviews_refuse(self):
        for age in [timedelta(hours=2),timedelta(seconds=-1)]:
            self.captured['checked_at'] = (self.now-age).isoformat()
            with self.subTest(age=age), self.assertRaises(ValueError): self.review()
        self.captured['checked_at'] = self.now.isoformat()
        for reviewed,expires in [(self.now-timedelta(days=32),self.now-timedelta(seconds=1)),
                                 (self.now,self.now+timedelta(days=32))]:
            self.catalogue.update(reviewed_at=reviewed.isoformat(),valid_until=expires.isoformat())
            with self.assertRaises(ValueError): self.review()
        self.catalogue['valid_until'] = self.now.replace(tzinfo=None).isoformat()
        with self.assertRaises(ValueError): self.review()

    def test_unknown_dispositions_and_missing_resolved_feature_evidence_refuse(self):
        self.catalogue['advisories'][0]['assessment'] = 'ignored'
        with self.assertRaises(ValueError): self.review()
        self.catalogue['advisories'][0]['assessment'] = 'fixed-in-selected-source'
        self.features.pop('MBEDTLS_SSL_SRV_C')
        with self.assertRaises(ValueError): self.review()

    def test_real_macro_parser_refuses_client_padding_rsa_and_private_values(self):
        baseline = b'#define MBEDTLS_SSL_SRV_C\n#define PSA_WANT_ALG_CHACHA20_POLY1305 1\n'
        self.assertEqual(audit.features(baseline), {'MBEDTLS_SSL_SRV_C':'','PSA_WANT_ALG_CHACHA20_POLY1305':'1'})
        for addition in [b'#define MBEDTLS_SSL_CLI_C\n',b'#define MBEDTLS_RSA_C\n',b'#define MBEDTLS_AES_C\n',b'#define PSA_WANT_ALG_GCM 1\n',b'#define MBEDTLS_CIPHER_MODE_CBC\n',
                         b'#define PSA_WANT_ALG_CHACHA20_POLY1305 1\n',b'#define PSA_WANT_PRIVATE "/private/secret"\n']:
            with self.subTest(addition=addition), self.assertRaises(ValueError): audit.features(baseline+addition)

    def test_actual_compile_command_policy_rejects_injected_definitions_includes_and_optimizer_flags(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source'; build = Path(folder) / 'build'
            source.mkdir(); build.mkdir(); unit_source = source / 'library.c'
            args = ['/usr/bin/cc', '-O2', '-std=c99', '-DNDEBUG',
                    '-DMBEDTLS_CONFIG_FILE="' + str(ROOT / 'tools/device-identity/mbedtls_config.h') + '"',
                    '-DTF_PSA_CRYPTO_CONFIG_FILE="' + str(ROOT / 'tools/device-identity/crypto_config.h') + '"',
                    '-I' + str(source), '-o', 'unit.o', '-c', str(unit_source)]
            unit = dict(directory=str(build), file=str(unit_source), command=shlex.join(args))
            audit.command_policy(unit, Path('/usr/bin/cc'), source, build)
            for extra in ['-DMBEDTLS_SSL_CLI_C', '-UMBEDTLS_SSL_SRV_C', '-include', '@private-response',
                          '-mllvm', '-Xclang', '-Ofast', '-flto', '-Wp,-DMBEDTLS_SSL_CLI_C', '-I/private/unreviewed']:
                altered = dict(unit, command=shlex.join([*args,extra]))
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    audit.command_policy(altered, Path('/usr/bin/cc'), source, build)

    def test_config_header_aliases_resolve_to_the_exact_reviewed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'sdk';build=root/'build';source.mkdir();build.mkdir()
            alias=root/'repository-alias';alias.symlink_to(ROOT,target_is_directory=True)
            args=['/usr/bin/cc','-DNDEBUG',
                '-DMBEDTLS_CONFIG_FILE="'+str(alias/'tools/device-identity/mbedtls_config.h')+'"',
                '-DTF_PSA_CRYPTO_CONFIG_FILE="'+str(alias/'tools/device-identity/crypto_config.h')+'"',
                '-o','unit.o','-c',str(source/'unit.c')]
            unit=dict(directory=str(build),file=str(source/'unit.c'),command=shlex.join(args))
            audit.command_policy(unit,Path('/usr/bin/cc'),source,build)
            copied=root/'copied.h';copied.write_bytes((ROOT/'tools/device-identity/crypto_config.h').read_bytes())
            for value in ['"'+str(copied)+'"','"tools/device-identity/crypto_config.h"',
                          '"'+str(root/'missing.h')+'"',str(alias/'tools/device-identity/crypto_config.h')]:
                altered=args.copy();altered[3]='-DTF_PSA_CRYPTO_CONFIG_FILE='+value
                with self.subTest(value=value),self.assertRaises((ValueError,OSError)):
                    audit.command_policy(dict(unit,command=shlex.join(altered)),Path('/usr/bin/cc'),source,build)
            altered=[*args,args[3]]
            with self.assertRaises(ValueError):
                audit.command_policy(dict(unit,command=shlex.join(altered)),Path('/usr/bin/cc'),source,build)

    @unittest.skipUnless(all(shutil.which(name) for name in ['cc','ar','nm']), 'native archive observer requires host tools')
    def test_actual_archive_observer_distinguishes_aes_and_non_aes_symbols(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            for label,symbol,expected in [('aes','mbedtls_aes_init',False),('gcm','mbedtls_gcm_init',False),
                                           ('selected','owned_non_aes_fixture',True)]:
                source=folder/(label+'.c');source.write_text('void '+symbol+'(void) {}\n')
                obj=folder/(label+'.o');archive=folder/(label+'.a')
                subprocess.run(['cc','-c',str(source),'-o',str(obj)],capture_output=True,check=True,timeout=30)
                subprocess.run(['ar','rcs',str(archive),str(obj)],capture_output=True,check=True,timeout=30)
                self.assertIs(audit.no_software_aes(archive),expected)

    def test_nonempty_index_is_required_before_any_advisory_verdict(self):
        with patch.object(audit.upstream,'fetch',return_value=b'<div itemprop="articleBody">'+b'public '*30+b'</div>'):
            with self.assertRaises(ValueError): audit.capture()


if __name__ == '__main__': unittest.main()
