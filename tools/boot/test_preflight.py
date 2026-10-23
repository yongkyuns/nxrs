"""Actual Espressif virtual-reader/formatter controls; no physical device evidence."""
import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from espefuse.efuse.esp32s3.mem_definition import EfuseDefineFields
import espsecure
import preflight as p
import sign
import target_plan as target

sys.path.insert(0, str(target.ROOT / 'tests/boot'))
from efuse_fixture import MAC, virtual_summary, rooted


class PreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='nxrs-preflight-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        cls.plan = target.plan('esp32s3-camera-mcuboot')
        cls.authority = cls.root / 'authority'
        cls.authority.mkdir(mode=0o700)
        cls.pin = sign.initialize(cls.authority, cls.plan['settings'], os.urandom(48).hex().encode('ascii'))
        cls.key = sign.rsa.generate_private_key(public_exponent=65537, key_size=3072).public_key()
        cls.pem = cls.key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        cls.spki = cls.key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        cls.digest = espsecure._digest_sbv2_public_key(io.BytesIO(cls.pem))
        cls.public = cls.root / 'rom-public.pem'
        cls.public.write_bytes(cls.pem)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.summary = self.work / 'summary.json'
        self.summary.write_bytes(virtual_summary())
        self.summary.chmod(0o600)
        self.binary = self.work / 'loader.bin'
        self.binary.write_bytes(b'bounded-build-binding-fixture-not-an-executable')
        policy, app = sign.public_policy((self.authority / 'policy.json').read_bytes(), self.pin)
        self.build = dict(schema=1, scope='esp32s3-cross-build-only', platform='esp32s3-camera-mcuboot',
            cross_build_qualified=True, deployment_permitted=False, physical_qualified=False,
            rom_authenticated=False, protected_device_counter=False,
            public_policy_sha256=self.pin,
            public_key_pkcs1_sha256=target.digest(app.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.PKCS1)),
            configuration_sha256=self.plan['settings']['configuration_sha256'],
            binary_bytes=self.binary.stat().st_size, binary_sha256=target.digest(self.binary.read_bytes()))
        self.build_path = self.work / 'boot-build.json'
        self.build_path.write_bytes(target.canonical(self.build))
        self.args = argparse.Namespace(summary=self.summary, summary_sha256=target.digest(self.summary.read_bytes()),
            policy=self.authority/'policy.json', policy_sha256=self.pin, platform='esp32s3-camera-mcuboot',
            rom_root=self.public, rom_root_sha256=target.digest(self.spki), expected_mac=MAC,
            boot_build=self.build_path, boot_build_sha256=target.digest(self.build_path.read_bytes()),
            bootloader=self.binary, out=self.work/'assessment.json')

    def replace_summary(self, raw):
        self.summary.write_bytes(raw)
        self.args.summary_sha256 = target.digest(raw)

    def test_large_pinned_mixed_language_record_and_upper_bound(self):
        # A real mixed Rust/C record exceeds the former 128 KiB cap. Preserve
        # its independent pin and exact binary binding, with a finite new cap.
        self.build['rust_source_inventory_fixture'] = 'x' * (192 * 1024)
        raw = target.canonical(self.build)
        self.build_path.write_bytes(raw)
        self.args.boot_build_sha256 = target.digest(raw)
        report = p.make_report(self.args)
        self.assertIs(report['deployment_permitted'], False)
        self.build_path.write_bytes(b' ' * (1024 * 1024 + 1))
        self.args.boot_build_sha256 = target.digest(self.build_path.read_bytes())
        with self.assertRaises(ValueError):
            p.make_report(self.args)

    def test_frozen_schema_matches_every_actual_pinned_sdk_definition(self):
        definitions = EfuseDefineFields(None)
        groups = definitions.EFUSES + definitions.KEYBLOCKS + definitions.CALIBRATION_EFUSES + definitions.CALC
        expected = {f.name:[f.bit_len,f.block,f.word,f.pos,f.type,f.write_disable_bit,f.read_disable_bit] for f in groups}
        self.assertEqual(p.schema()['fields'], expected)
        self.assertEqual(set(p.schema()['required']), {f.name for f in definitions.EFUSES + definitions.KEYBLOCKS})

    def test_rom_digest_matches_real_vendor_cli_and_differs_from_application_and_operator_hashes(self):
        out = self.work/'rom-digest.bin'
        child = subprocess.run([sys.executable,'-m','espsecure','digest-sbv2-public-key',
            '--keyfile',str(self.public),'--output',str(out)], capture_output=True, timeout=10,
            env={k:os.environ[k] for k in ('PATH','HOME','TMPDIR') if k in os.environ})
        self.assertEqual(child.returncode, 0)
        self.assertEqual(out.read_bytes(), self.digest)
        self.assertNotEqual(self.digest.hex(), self.args.rom_root_sha256)
        self.assertNotEqual(self.digest, hashlib.sha256(self.key.public_bytes(serialization.Encoding.DER,serialization.PublicFormat.PKCS1)).digest())

    def test_actual_unprovisioned_staged_and_enabled_virtual_states_remain_review_only(self):
        for raw, state in ((virtual_summary(), 'unprovisioned-review-required'),
                           (rooted(self.digest,False), 'root-staged-review-required'),
                           (rooted(self.digest), 'secure-boot-root-observed')):
            self.replace_summary(raw)
            with patch('serial.Serial', side_effect=AssertionError('physical transport forbidden')):
                report = p.make_report(self.args)
            self.assertEqual(report['state'], state)
            for key in ('physical_qualified','deployment_permitted','device_accessed','irreversible_actions_performed',
                        'coding_errors_checked','capture_authenticity_verified','capture_freshness_verified',
                        'rom_signed_bootloader_verified','hardware_counter_provider_qualified','flash_key_custody_qualified'):
                self.assertIs(report[key], False)
            self.assertNotIn(MAC, json.dumps(report))

    def test_wrong_hidden_writable_revoked_duplicate_and_extra_active_roots_are_refused(self):
        cases = [rooted(bytes([9])*32), rooted(self.digest,hidden=['BLOCK_KEY0']),
                 rooted(self.digest,protected=['KEY_PURPOSE_0','RD_DIS']),
                 rooted(self.digest,protected=['BLOCK_KEY0','RD_DIS']),
                 rooted(self.digest,changes={'SECURE_BOOT_KEY_REVOKE0':1}),
                 rooted(self.digest,changes={'KEY_PURPOSE_1':9}),
                 rooted(self.digest,False,changes={'SECURE_BOOT_EN':1}),
                 rooted(self.digest,protected=['BLOCK_KEY0','KEY_PURPOSE_0']),
                 rooted(self.digest,changes={'SPI_BOOT_CRYPT_CNT':1})]
        for raw in cases:
            with self.subTest(sha256=target.digest(raw)):
                with self.assertRaises(ValueError):
                    p.assess(raw,MAC,self.digest)

    def test_actual_sdk_calibration_variant_and_counter_bits_remain_separate(self):
        raw=virtual_summary(dict(BLK_VERSION_MAJOR=1,SECURE_VERSION=15))
        fields,_=p.parse_summary(raw)
        self.assertEqual(len(fields),112)
        observed=p.assess(raw,MAC,self.digest)
        self.assertEqual(observed['secure_version_raw'],15)
        self.assertEqual(observed['secure_version_programmed_bits'],4)

    def test_permission_labels_must_match_actual_protection_registers(self):
        original = json.loads(rooted(self.digest))
        for name,key,value in (('BLOCK_KEY0','writeable',True),('KEY_PURPOSE_0','writeable',True),
                               ('BLOCK_KEY0','readable',False),('WR_DIS','raw_value','0x00000000')):
            changed=copy.deepcopy(original);changed[name][key]=value
            with self.assertRaises(ValueError): p.assess(target.canonical(changed),MAC,self.digest)

    def test_incomplete_unknown_other_chip_metadata_bad_types_and_inconsistent_values_are_refused(self):
        original=json.loads(virtual_summary())
        malformed=[]
        changed=copy.deepcopy(original);del changed['RD_DIS'];malformed.append(changed)
        changed=copy.deepcopy(original);changed['unqualified-field']={};malformed.append(changed)
        for name,key,value in (('SECURE_BOOT_EN','pos',19),('SECURE_BOOT_EN','bit_len',True),
                               ('SECURE_BOOT_EN','value',True),('KEY_PURPOSE_0','value','SECURE_BOOT_DIGEST0'),
                               ('MAC','value',MAC+' (not OK)'),('SECURE_VERSION','value',True),
                               ('RD_DIS','raw_value','0xff'),('RD_DIS','readable',1)):
            changed=copy.deepcopy(original);changed[name][key]=value;malformed.append(changed)
        for changed in malformed:
            with self.assertRaises(ValueError): p.assess(target.canonical(changed),MAC,self.digest)
        with self.assertRaises(ValueError): p.document(b'{"x":1,"x":2}')
        with self.assertRaises(ValueError): p.document(b'{"x":NaN}')
        with self.assertRaises(ValueError): p.document(b'{"x":1e999}')

    def test_expected_mac_root_policy_build_and_binary_pins_are_independent(self):
        for name,value in (('expected_mac','02:00:12:34:56:79'),('summary_sha256','0'*64),
                           ('rom_root_sha256','0'*64),('policy_sha256','0'*64),('boot_build_sha256','0'*64)):
            changed=copy.copy(self.args);setattr(changed,name,value)
            with self.assertRaises(ValueError): p.make_report(changed)
        old=self.binary.read_bytes();self.binary.write_bytes(old+b'changed')
        with self.assertRaises(ValueError): p.make_report(self.args)

    def test_application_root_reuse_private_pem_and_trailing_material_are_refused(self):
        policy,app=sign.public_policy(self.args.policy.read_bytes(),self.pin)
        reused=self.work/'reused.pem';reused.write_bytes(app.public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo))
        changed=copy.copy(self.args);changed.rom_root=reused
        changed.rom_root_sha256=target.digest(app.public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo))
        with self.assertRaises(ValueError): p.make_report(changed)
        private=sign.rsa.generate_private_key(public_exponent=65537,key_size=3072)
        reused.write_bytes(private.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(os.urandom(32))))
        with self.assertRaises(ValueError):p.make_report(changed)
        reused.write_bytes(self.pem+b'private-data-must-not-appear-in-diagnostics')
        changed.rom_root_sha256=self.args.rom_root_sha256
        with self.assertRaises(ValueError): p.make_report(changed)

    def test_rebound_build_metadata_and_scope_forgery_are_refused(self):
        for name,value in (('public_policy_sha256','0'*64),('configuration_sha256','0'*64),
                           ('public_key_pkcs1_sha256','0'*64),('binary_bytes',True),
                           ('deployment_permitted',True),('rom_authenticated',True)):
            changed=dict(self.build,**{name:value});raw=target.canonical(changed)
            self.build_path.write_bytes(raw);self.args.boot_build_sha256=target.digest(raw)
            with self.assertRaises(ValueError): p.make_report(self.args)

    def test_unrelated_secret_key_bytes_never_enter_report_and_output_is_exclusive_private(self):
        sentinel=b'private-HMAC-material-not-output'
        self.assertEqual(len(sentinel),32)
        self.replace_summary(virtual_summary(dict(KEY_PURPOSE_1=8,BLOCK_KEY1=int.from_bytes(sentinel,'little'))))
        report=p.run(self.args)
        out=self.args.out.read_bytes()
        for secret in (sentinel,sentinel.hex().encode(),sentinel.hex(' ').encode(),MAC.encode()): self.assertNotIn(secret,out)
        self.assertEqual(report['non_boot_key_blocks_occupied'],1)
        self.assertEqual(self.args.out.stat().st_mode & 0o777,0o600)
        with self.assertRaises(FileExistsError):p.run(self.args)
        self.assertEqual(out,self.args.out.read_bytes())

    def test_private_summary_symlink_hardlink_fifo_and_permissive_modes_are_refused(self):
        for kind in ('symlink','hardlink','fifo'):
            alias=self.work/kind
            if kind=='symlink':alias.symlink_to(self.summary)
            elif kind=='hardlink':os.link(self.summary,alias)
            else:os.mkfifo(alias,0o600)
            args=copy.copy(self.args);args.summary=alias
            with self.assertRaises((OSError,ValueError)):p.make_report(args)
            alias.unlink()
        self.summary.chmod(0o644)
        with self.assertRaises(ValueError):p.make_report(self.args)

    def test_source_changes_and_oversized_snapshots_are_refused(self):
        original=p.source_inputs()
        with patch.object(p,'source_inputs',side_effect=[original,dict(original,changed='0'*64)]):
            with self.assertRaisesRegex(p.Refusal,'^source-changed$'):p.make_report(self.args)
        self.summary.write_bytes(b' '*(128*1024+1))
        self.args.summary_sha256=target.digest(self.summary.read_bytes())
        with self.assertRaises(ValueError):p.make_report(self.args)

    def test_output_aliases_preserve_existing_reports(self):
        target_file=self.work/'existing'
        target_file.write_bytes(b'unrelated-must-survive')
        for name,make in (('symlink',lambda a:a.symlink_to(target_file)),('hardlink',lambda a:os.link(target_file,a))):
            alias=self.work/name
            make(alias)
            self.args.out=alias
            with self.assertRaises(FileExistsError):p.run(self.args)
            self.assertEqual(target_file.read_bytes(),b'unrelated-must-survive')
            alias.unlink()

    def test_cli_diagnostics_do_not_echo_private_paths_or_device_write_options(self):
        private='private-operator-input-must-not-be-logged'
        for arguments in (['--port',private], ['--burn-efuse',private], ['--summary',private]):
            child=subprocess.run([sys.executable,str(target.ROOT/'tools/boot/preflight.py'),*arguments],
                                 capture_output=True,timeout=10)
            self.assertEqual(child.returncode,2)
            self.assertEqual(child.stderr,b'Preflight invocation refused\n')
            self.assertNotIn(private.encode(),child.stdout+child.stderr)


if __name__=='__main__':unittest.main()
