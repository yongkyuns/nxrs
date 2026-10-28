#!/usr/bin/env python3
"""Prepare, assemble and verify an exact ESP32-S3 ROM-signed bootloader offline."""
import argparse
from contextlib import redirect_stdout
import io
from pathlib import Path
import struct
import sys
import zlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
import espsecure
from esptool.bin_image import ESP32S3FirmwareImage

from boot_artifacts import Refusal, need, pinned, approved_boot
import sign
import target_plan as target

SECTOR = 4096
BLOCK = 1216


def source_inputs():
    return {name:target.digest((target.ROOT/name).read_bytes()) for name in (
        'tools/boot/rom_image.py', 'tools/boot/boot_artifacts.py', 'tools/boot/sign.py',
        'tools/boot/target_plan.py', 'tools/boot/build_esp32s3.py',
        'tools/boot/qualify.py', 'tools/platform_config.py',
        'security/esp32s3-boot-dependencies.json')}


def contents(approved):
    # An extra complete signature sector must fit before any signing request is
    # exported. The pinned build record binds the original bytes and target.
    limit = min(approved.plan['configuration']['boot']['bootloader_bytes'],
                approved.plan['settings']['layout']['primary_offset'])
    body = approved.binary
    try:
        with redirect_stdout(io.StringIO()):
            image = ESP32S3FirmwareImage(io.BytesIO(body))
        need(image.chip_id == image.ROM_LOADER.IMAGE_CHIP_ID
             and image.append_digest and image.stored_digest == image.calc_digest
             and image.checksum == image.calculate_checksum()
             and image.data_length + 32 == len(body), 'rom-loader-format')
    except Exception:
        raise Refusal('rom-loader-format') from None
    padded = len(body) + (-len(body) % SECTOR)
    need(0 < len(body) and padded + SECTOR <= limit, 'rom-container-budget')
    return body + b'\xff' * (padded-len(body))


def request(approved):
    body = contents(approved)
    return dict(schema=1, role='nxrs-esp32s3-rom-bootloader-signing-request',
        platform=approved.plan['settings']['target'], chip='esp32s3',
        policy_sha256=target.digest(approved.policy),
        configuration_sha256=approved.plan['settings']['configuration_sha256'],
        boot_build_sha256=target.digest(approved.build_bytes),
        unsigned_bootloader_sha256=target.digest(approved.binary),
        rom_root_spki_sha256=target.digest(approved.spki),
        rom_root_efuse_digest=approved.rom_digest.hex(),
        algorithm='RSA-3072-PSS', digest_algorithm='SHA-256',
        mgf_digest_algorithm='SHA-256', salt_bytes=32,
        signing_input='prehashed-sha256', signature_encoding='384-byte-big-endian',
        digest_to_sign=target.digest(body), content_bytes=len(body),
        signed_container_bytes=len(body)+SECTOR)


def verify(image, approved):
    body = contents(approved)
    need(len(image) == len(body)+SECTOR and image[:len(body)] == body, 'rom-container-content')
    block = image[len(body):len(body)+BLOCK]
    need(block[:4] == b'\xe7\x02\x00\x00' and block[1200:] == bytes(16)
         and image[len(body)+BLOCK:] == b'\xff'*(SECTOR-BLOCK), 'rom-container-format')
    need(struct.unpack_from('<I',block,1196)[0] == zlib.crc32(block[:1196]) & 0xffffffff, 'rom-container-crc')
    need(block[4:36].hex() == target.digest(body), 'rom-container-digest')
    # Host espsecure verify-signature uses its supplied public key. ROM uses the
    # embedded hardware representation as well: bind every N/E/R/M byte to the
    # independently approved fuse digest, including Montgomery coefficients.
    need(target.digest(block[36:812]) == approved.rom_digest.hex(), 'rom-container-root')
    try:
        approved.key.verify(block[812:1196][::-1],body,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=32),hashes.SHA256())
    except InvalidSignature:
        raise Refusal('rom-container-signature') from None
    return dict(schema=1, scope='host-verified-rom-signed-bootloader-artifact',
        signed_bootloader_sha256=target.digest(image), signed_bootloader_bytes=len(image),
        request_sha256=target.digest(target.canonical(request(approved))+b'\n'),
        rom_root_spki_sha256=target.digest(approved.spki), rom_root_efuse_digest=approved.rom_digest.hex(),
        boot_build_sha256=target.digest(approved.build_bytes),
        policy_sha256=target.digest(approved.policy),
        configuration_sha256=approved.plan['settings']['configuration_sha256'],
        rom_signed_bootloader_verified=True, rom_execution_qualified=False,
        build_provenance_authenticated=False, protected_counter_qualified=False,
        signing_key_custody_qualified=False, physical_qualified=False, deployment_permitted=False)


def assemble(signature, approved):
    need(len(signature) == 384, 'rom-signature-size')
    body = contents(approved)
    # The established vendor encoder verifies the external signature first. Only
    # public keys/signatures enter it; this tool never handles a private root.
    try:
        with redirect_stdout(io.StringIO()):
            block = espsecure.generate_signature_block_using_pre_calculated_signature(
                [io.BytesIO(signature)],[io.BytesIO(approved.public)],body)
    except Exception:
        raise Refusal('rom-external-signature') from None
    need(len(block) == BLOCK, 'rom-vendor-format')
    image = body + block + b'\xff'*(SECTOR-BLOCK)
    verify(image,approved)
    return image


def run(args):
    inputs = source_inputs()
    approved = approved_boot(args)
    if args.operation == 'prepare':
        output = target.canonical(request(approved))+b'\n'
        result = dict(request_sha256=target.digest(output))
    elif args.operation == 'assemble':
        raw = pinned(sign.untrusted_read(args.request,8192),args.request_sha256)
        need(raw == target.canonical(request(approved))+b'\n', 'rom-signing-request')
        output = assemble(sign.untrusted_read(args.signature,384),approved)
        result = verify(output,approved)
    else:
        need(args.operation == 'verify','rom-operation')
        limit = approved.plan['configuration']['boot']['bootloader_bytes']
        result = verify(sign.untrusted_read(args.image,limit),approved)
        result['tool_inputs'] = inputs
        output = target.canonical(result)+b'\n'
    need(inputs == source_inputs(), 'source-changed')
    sign.publish(args.out,output)
    return result


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2,'ROM artifact invocation refused\n')


def main():
    parser = Parser(description=__doc__)
    commands = parser.add_subparsers(dest='operation',required=True,parser_class=Parser)
    for operation in ('prepare','assemble','verify'):
        command = commands.add_parser(operation)
        for name in ('policy','rom-root','boot-build','bootloader','out'):
            command.add_argument('--'+name,type=Path,required=True)
        for name in ('platform','policy-sha256','rom-root-sha256','boot-build-sha256'):
            command.add_argument('--'+name,required=True)
        if operation == 'assemble':
            command.add_argument('--request',type=Path,required=True)
            command.add_argument('--request-sha256',required=True)
            command.add_argument('--signature',type=Path,required=True)
        if operation == 'verify':
            command.add_argument('--image',type=Path,required=True)
    args = parser.parse_args()
    try:
        result = run(args)
    except Exception as error:
        category = str(error) if isinstance(error,Refusal) else 'input-or-runtime'
        print('ROM artifact refused: '+category,file=sys.stderr)
        return 1
    if args.operation == 'prepare':
        print('ROM signing request '+result['request_sha256'])
    else:
        print('ROM artifact '+args.operation+' passed; physical gates remain open')
    return 0


if __name__=='__main__':raise SystemExit(main())
