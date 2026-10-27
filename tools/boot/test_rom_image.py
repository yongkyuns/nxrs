"""Actual vendor-format signed containers; these are not physical ROM tests."""
import argparse
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, utils
from esptool.bin_image import ESP32S3FirmwareImage, ImageSegment
import espsecure

from boot_artifacts import approved_boot, Refusal
import preflight
import rom_image as rom
import sign
import target_plan as target
sys.path.insert(0,str(target.ROOT/'tests/boot'))
from efuse_fixture import MAC, rooted


class RomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory(prefix='nxrs-rom-controls-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root=Path(cls.temporary.name).resolve()
        cls.plan=target.plan('esp32s3-camera-mcuboot')
        cls.authority=cls.root/'authority';cls.authority.mkdir(mode=0o700)
        cls.pin=sign.initialize(cls.authority,cls.plan['settings'],os.urandom(48).hex().encode('ascii'))
        cls.private=sign.rsa.generate_private_key(public_exponent=65537,key_size=3072)
        cls.public=cls.private.public_key()
        cls.pem=cls.public.public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
        cls.key_path=cls.root/'public.pem';cls.key_path.write_bytes(cls.pem)
        image=ESP32S3FirmwareImage()
        image.chip_id=image.ROM_LOADER.IMAGE_CHIP_ID
        image.entrypoint=0x40370000
        image.segments=[ImageSegment(0x40370000,bytes(range(256))*4)]
        image.segments[0].name='.text'
        with contextlib.redirect_stdout(io.StringIO()):cls.binary=image.save(None)
        _,app=sign.public_policy((cls.authority/'policy.json').read_bytes(),cls.pin)
        cls.build=dict(schema=1,scope='esp32s3-cross-build-only',platform='esp32s3-camera-mcuboot',
            cross_build_qualified=True,deployment_permitted=False,physical_qualified=False,
            rom_authenticated=False,protected_device_counter=False,public_policy_sha256=cls.pin,
            public_key_pkcs1_sha256=target.digest(app.public_bytes(serialization.Encoding.DER,serialization.PublicFormat.PKCS1)),
            configuration_sha256=cls.plan['settings']['configuration_sha256'],
            binary_sha256=target.digest(cls.binary),binary_bytes=len(cls.binary))

    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(dir=self.root);self.addCleanup(self.temporary.cleanup)
        self.work=Path(self.temporary.name)
        self.loader=self.work/'loader.bin';self.loader.write_bytes(self.binary)
        self.build_path=self.work/'build.json';self.build_path.write_bytes(target.canonical(self.build))
        self.args=argparse.Namespace(platform='esp32s3-camera-mcuboot',policy=self.authority/'policy.json',
            policy_sha256=self.pin,rom_root=self.key_path,
            rom_root_sha256=target.digest(self.public.public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)),
            boot_build=self.build_path,boot_build_sha256=target.digest(self.build_path.read_bytes()),
            bootloader=self.loader,out=self.work/'output',operation='prepare')
        self.approved=approved_boot(self.args)
        self.body=rom.contents(self.approved)
        self.signature=self.private.sign(bytes.fromhex(rom.request(self.approved)['digest_to_sign']),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=32),utils.Prehashed(hashes.SHA256()))
        self.good=rom.assemble(self.signature,self.approved)

    def mutated(self,offset,value=1,crc=True):
        data=bytearray(self.good);data[len(self.body)+offset]^=value
        if crc:struct.pack_into('<I',data,len(self.body)+1196,zlib.crc32(data[len(self.body):len(self.body)+1196])&0xffffffff)
        return bytes(data)

    def test_external_request_assembly_verification_and_idempotent_publication(self):
        result=rom.run(self.args);request=self.args.out.read_bytes()
        self.assertEqual(result['request_sha256'],target.digest(request))
        spec=json.loads(request)
        self.assertEqual(spec['signing_input'],'prehashed-sha256')
        self.assertEqual(spec['digest_to_sign'],target.digest(self.body))
        self.assertEqual(spec['signature_encoding'],'384-byte-big-endian')
        self.args.request=self.args.out;self.args.request_sha256=target.digest(request)
        self.args.signature=self.work/'signature';self.args.signature.write_bytes(self.signature)
        self.args.out=self.work/'signed.bin';self.args.operation='assemble'
        rom.run(self.args);rom.run(self.args)
        self.assertEqual(self.args.out.read_bytes(),self.good)
        self.assertEqual(self.args.out.stat().st_mode&0o777,0o600)
        self.args.image=self.args.out;self.args.out=self.work/'verified.json';self.args.operation='verify'
        result=rom.run(self.args)
        self.assertTrue(result['rom_signed_bootloader_verified'])
        self.assertFalse(result['rom_execution_qualified'])
        self.assertFalse(result['deployment_permitted'])

    def test_all_header_key_coefficient_signature_padding_and_crc_corruptions_refuse(self):
        for offset in (0,1,2,3,4,35,36,419,420,423,424,807,808,811,812,1195,1196,1199,1200,1215,1216,4095):
            with self.subTest(offset=offset):
                with self.assertRaises(ValueError):rom.verify(self.mutated(offset,crc=offset<1196),self.approved)
        for data in (self.good[:-1],self.good+b'\xff',b'\0'+self.good[1:],self.good[:len(self.body)]+self.good[len(self.body):]*2):
            with self.assertRaises(ValueError):rom.verify(data,self.approved)

    def test_coherent_hardware_key_mutations_pass_supplied_key_vendor_check_but_fail_root_binding(self):
        for offset in (36,424,808):
            changed=self.mutated(offset)
            with contextlib.redirect_stdout(io.StringIO()):
                espsecure.verify_signature_v2(False,None,io.BytesIO(self.pem),io.BytesIO(changed))
            with self.assertRaisesRegex(Refusal,'^rom-container-root$'):rom.verify(changed,self.approved)

    def test_other_root_wrong_message_wrong_salt_and_short_signatures_are_refused(self):
        other=sign.rsa.generate_private_key(public_exponent=65537,key_size=3072)
        signatures=[self.signature[:-1],self.signature+b'\0',other.sign(self.body,padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=32),hashes.SHA256()),
            self.private.sign(self.body,padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=31),hashes.SHA256()),
            self.private.sign(b'wrong-image',padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=32),hashes.SHA256())]
        for signature in signatures:
            with self.assertRaises(ValueError):rom.assemble(signature,self.approved)

    def test_signature_sector_budget_is_checked_before_vendor_or_signer_use(self):
        plan=copy.deepcopy(self.approved.plan)
        plan['configuration']['boot']['bootloader_bytes']=len(self.body)+4095
        from dataclasses import replace
        with patch.object(rom.espsecure,'generate_signature_block_using_pre_calculated_signature',side_effect=AssertionError('must not encode')):
            with self.assertRaisesRegex(Refusal,'^rom-container-budget$'):
                rom.assemble(self.signature,replace(self.approved,plan=plan))
        plan['configuration']['boot']['bootloader_bytes']=len(self.body)+4096
        plan['settings']['layout']['primary_offset']=len(self.body)+4095
        with self.assertRaisesRegex(Refusal,'^rom-container-budget$'):rom.request(replace(self.approved,plan=plan))

    def test_malformed_other_chip_checksum_and_appended_image_hash_are_refused(self):
        from dataclasses import replace
        other=bytearray(self.binary)
        other[12]^=1
        import hashlib
        other[-32:]=hashlib.sha256(other[:-32]).digest()
        for raw in (b'\x7fELF'+self.binary[4:],self.binary+b'\xff',self.binary[:-1]+bytes([self.binary[-1]^1]),bytes(other)):
            with self.assertRaisesRegex(Refusal,'^rom-loader-format$'):rom.contents(replace(self.approved,binary=raw))

    def test_request_pin_changes_unknown_fields_and_external_signature_aliases_preserve_outputs(self):
        spec=target.canonical(rom.request(self.approved))+b'\n'
        self.args.operation='assemble';self.args.request=self.work/'request';self.args.request.write_bytes(spec)
        self.args.request_sha256='0'*64
        self.args.signature=self.work/'signature';self.args.signature.write_bytes(self.signature)
        with self.assertRaises(ValueError):rom.run(self.args)
        changed=json.loads(spec);changed['role']='other-role';self.args.request.write_bytes(target.canonical(changed)+b'\n')
        self.args.request_sha256=target.digest(self.args.request.read_bytes())
        with self.assertRaisesRegex(Refusal,'^rom-signing-request$'):rom.run(self.args)
        self.args.request.write_bytes(spec);self.args.request_sha256=target.digest(spec)
        original=self.args.signature;alias=self.work/'alias';alias.symlink_to(original);self.args.signature=alias
        with self.assertRaises(OSError):rom.run(self.args)
        alias.unlink();os.mkfifo(alias,0o600)
        with self.assertRaises(ValueError):rom.run(self.args)
        self.assertFalse(self.args.out.exists())

    def test_preflight_checks_signed_artifact_against_same_fuse_root_and_build(self):
        self.args.summary=self.work/'summary';self.args.summary.write_bytes(rooted(self.approved.rom_digest));self.args.summary.chmod(0o600)
        self.args.summary_sha256=target.digest(self.args.summary.read_bytes());self.args.expected_mac=MAC
        self.args.rom_image=self.work/'signed';self.args.rom_image.write_bytes(self.good)
        result=preflight.make_report(self.args)
        self.assertTrue(result['rom_signed_bootloader_verified'])
        self.assertEqual(result['rom_signed_bootloader_sha256'],target.digest(self.good))
        self.assertFalse(result['rom_execution_qualified'])
        self.assertFalse(result['deployment_permitted'])
        self.args.rom_image.write_bytes(self.mutated(424))
        with self.assertRaisesRegex(Refusal,'^rom-container-root$'):preflight.make_report(self.args)

    def test_output_aliases_existing_foreign_files_and_source_changes_are_refused(self):
        self.args.out.write_bytes(b'unrelated-file')
        with self.assertRaises(ValueError):rom.run(self.args)
        self.assertEqual(self.args.out.read_bytes(),b'unrelated-file')
        self.args.out.unlink()
        original=rom.source_inputs()
        with patch.object(rom,'source_inputs',side_effect=[original,dict(original,changed='0'*64)]):
            with self.assertRaisesRegex(Refusal,'^source-changed$'):rom.run(self.args)
        self.assertFalse(self.args.out.exists())

    def test_command_line_does_not_accept_private_keys_device_actions_or_echo_inputs(self):
        sentinel='private-operator-value-not-for-logs'
        for operation in ('prepare','assemble','verify'):
            for option in ('--port','--private-key','--burn-efuse','--reset'):
                child=subprocess.run([sys.executable,str(target.ROOT/'tools/boot/rom_image.py'),operation,option,sentinel],capture_output=True,timeout=10)
                self.assertEqual(child.returncode,2)
                self.assertEqual(child.stderr,b'ROM artifact invocation refused\n')
                self.assertNotIn(sentinel.encode(),child.stdout+child.stderr)
        options=vars(self.args) | {'rom_root':self.work/sentinel}
        command=[sys.executable,str(target.ROOT/'tools/boot/rom_image.py'),'prepare']
        for name,value in options.items():
            if name != 'operation':command.extend(('--'+name.replace('_','-'),str(value)))
        child=subprocess.run(command,capture_output=True,timeout=10)
        self.assertEqual(child.returncode,1)
        self.assertEqual(child.stderr,b'ROM artifact refused: input-or-runtime\n')
        self.assertNotIn(sentinel.encode(),child.stdout+child.stderr)


if __name__=='__main__':unittest.main()
