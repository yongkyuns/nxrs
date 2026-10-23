#!/usr/bin/env python3
"""Offline ESP32-S3 manufacturing assessment; no device or write-command access."""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sys

from cryptography.hazmat.primitives import serialization
import espsecure

import build_esp32s3 as builder
import sign
import target_plan as target

SCHEMA = target.ROOT / 'security/esp32s3-efuse-summary-schema.json'
SCHEMA_SHA256 = 'e5ea07b06bce5cd710a07d48d5ed3d0e4c7df1e4b7c5bb1643aa0654bb2f525a'
META = ('bit_len', 'block', 'word', 'pos', 'efuse_type')
ROW = set(META) | {'name', 'value', 'raw_value', 'readable', 'writeable', 'description', 'category'}
PURPOSES = ('USER', 'RESERVED', 'XTS_AES_256_KEY_1', 'XTS_AES_256_KEY_2', 'XTS_AES_128_KEY',
            'HMAC_DOWN_ALL', 'HMAC_DOWN_JTAG', 'HMAC_DOWN_DIGITAL_SIGNATURE', 'HMAC_UP',
            'SECURE_BOOT_DIGEST0', 'SECURE_BOOT_DIGEST1', 'SECURE_BOOT_DIGEST2')


class Refusal(ValueError):
    """Fixed public category. Never includes input data or SDK exception text."""


def need(value, category):
    if not value:
        raise Refusal(category)


def pairs(items):
    result = {}
    for key, value in items:
        need(key not in result, 'duplicate-json')
        result[key] = value
    return result


def nonfinite(_):
    raise Refusal('nonfinite-json')


def finite_float(text):
    value = float(text)
    need(math.isfinite(value), 'nonfinite-json')
    return value


def document(raw):
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite, parse_float=finite_float)


def pinned(raw, expected):
    need(re.fullmatch('[0-9a-f]{64}', expected) is not None and target.digest(raw) == expected, 'input-pin')
    return raw


def schema():
    return document(pinned(sign.untrusted_read(SCHEMA, 32768), SCHEMA_SHA256))


def parse_summary(raw):
    table = schema()
    fields = document(raw)
    need(type(fields) is dict and set(table['required']) <= fields.keys() <= table['fields'].keys(), 'summary-fields')
    values = {}
    for name, row in fields.items():
        expected = table['fields'][name]
        need(type(row) is dict and set(row) == ROW and row['name'] == name, 'summary-shape')
        need([row[k] for k in META] == expected[:5]
             and all(type(row[k]) is type(v) for k, v in zip(META, expected)), 'summary-chip-schema')
        need(type(row['readable']) is bool and type(row['writeable']) is bool, 'summary-permissions')
        need((type(row['value']) in (str, int, bool) or name == 'TEMP_CALIB' and type(row['value']) is float)
             and len(str(row['value'])) <= 1024, 'summary-value')
        need(all(type(row[k]) is str and len(row[k]) <= 1024 for k in ('description', 'category', 'raw_value')), 'summary-text')
        bits = expected[0]
        need(re.fullmatch('0x[0-9a-f]{' + str((bits + 3)//4) + '}', row['raw_value']) is not None, 'summary-raw')
        value = int(row['raw_value'], 16)
        need(value < 1 << bits, 'summary-range')
        # Six SDK boolean fields use textual meanings. Security decisions below
        # use bounded raw bits, never arbitrary human-readable permission labels.
        if expected[4] == 'bool' and name not in {
            'DIS_USB_SERIAL_JTAG_ROM_PRINT', 'FLASH_ECC_MODE', 'FLASH_TYPE',
            'PIN_POWER_SELECTION', 'USB_PHY_SEL', 'VDD_SPI_TIEH',
        }:
            need(type(row['value']) is bool and row['value'] == bool(value), 'summary-boolean')
        values[name] = value
    for register in ('WR_DIS', 'RD_DIS'):
        need(type(fields[register]['value']) is int and fields[register]['value'] == values[register], 'summary-register-values')
    # Reconstruct permissions from the actual raw protection registers. Trusting
    # a forged R/- label alone could turn a writable root into an approved one.
    for name, row in fields.items():
        write_bit, read_bit = table['fields'][name][5:]
        need(read_bit is None or type(read_bit) is int, 'summary-read-mask')
        readable = read_bit is None or values['RD_DIS'] & (1 << read_bit) == 0
        writeable = write_bit is None or values['WR_DIS'] & (1 << write_bit) == 0
        need(row['readable'] == readable and row['writeable'] == writeable, 'summary-protection-registers')
    return fields, values


def assess(raw, expected_mac, digest):
    fields, values = parse_summary(raw)
    need(re.fullmatch('(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', expected_mac) is not None, 'device-identity')
    mac = bytes.fromhex(expected_mac.replace(':', ''))
    need(mac != bytes(6) and not mac[0] & 1, 'device-identity')
    need(fields['MAC']['readable'] and fields['MAC']['value'] == expected_mac + ' (OK)'
         and fields['MAC']['raw_value'] == '0x' + mac[::-1].hex(), 'device-identity')
    need(values['SPI_BOOT_CRYPT_CNT'] == 0, 'unsupported-encrypted-flash-state')
    need(fields['SPI_BOOT_CRYPT_CNT']['value'] == 'Disable', 'summary-encryption')
    need(type(fields['SECURE_VERSION']['value']) is int
         and fields['SECURE_VERSION']['value'] == values['SECURE_VERSION'], 'summary-security-version')
    slots = {}
    other_keys = 0
    for index in range(6):
        purpose_name = 'KEY_PURPOSE_' + str(index)
        purpose = values[purpose_name]
        need(purpose < len(PURPOSES) and fields[purpose_name]['value'] == PURPOSES[purpose], 'key-purpose')
        block = fields['BLOCK_KEY' + str(index)]
        if purpose < 9:
            other_keys += int(purpose != 0 or not block['readable'] or values[block['name']] != 0)
            continue
        slot = purpose - 9
        need(slot not in slots, 'duplicate-root-slot')
        need(block['readable'], 'hidden-boot-digest')
        block_bytes = bytes.fromhex(block['raw_value'][2:])
        need(block['value'] == block_bytes.hex(' '), 'summary-digest-bytes')
        need(not block['writeable'] and not fields[purpose_name]['writeable'], 'mutable-boot-root')
        revoked = bool(values['SECURE_BOOT_KEY_REVOKE' + str(slot)])
        if not revoked:
            need(block_bytes == digest, 'unapproved-boot-root')
        slots[slot] = {'key_block': index, 'revoked': revoked, 'approved_root_matches': block_bytes == digest}
    active = [slot for slot, state in slots.items() if not state['revoked']]
    need(len(active) <= 1, 'unsupported-multiple-active-roots')
    available = [slot for slot in range(3) if not values['SECURE_BOOT_KEY_REVOKE' + str(slot)]]
    need(available, 'all-boot-roots-revoked')
    enabled = bool(values['SECURE_BOOT_EN'])
    if enabled:
        need(len(active) == 1 and available == active, 'enabled-boot-root-set')
        need(not fields['RD_DIS']['writeable'], 'boot-digest-read-protection-unlocked')
    return {
        'device_mac_sha256': target.digest(mac),
        'state': 'secure-boot-root-observed' if enabled else ('root-staged-review-required' if active else 'unprovisioned-review-required'),
        'secure_boot_enabled_observed': enabled,
        'boot_roots': {str(slot): state for slot, state in sorted(slots.items())},
        'non_boot_key_blocks_occupied': other_keys,
        'secure_version_raw': values['SECURE_VERSION'],
        'secure_version_programmed_bits': values['SECURE_VERSION'].bit_count(),
        'debug_download_observed': {name: values[name] for name in (
            'DIS_PAD_JTAG', 'DIS_USB_JTAG', 'DIS_USB_SERIAL_JTAG', 'SOFT_DIS_JTAG',
            'DIS_DOWNLOAD_MODE', 'ENABLE_SECURITY_DOWNLOAD', 'DIS_DIRECT_BOOT',
            'DIS_USB_OTG_DOWNLOAD_MODE', 'DIS_USB_SERIAL_JTAG_DOWNLOAD_MODE')},
    }


def source_inputs():
    return {name: target.digest((target.ROOT/name).read_bytes()) for name in (
        'tools/boot/preflight.py', 'tools/boot/sign.py', 'tools/boot/target_plan.py',
        'tools/boot/build_esp32s3.py', 'security/esp32s3-boot-dependencies.json',
        'security/esp32s3-efuse-summary-schema.json')}


def make_report(args):
    inputs = source_inputs()
    builder.checked_environment()
    summary = pinned(sign.private_read(args.summary, 128*1024), args.summary_sha256)
    plan, policy, application_key = target.checked_policy(args.platform, args.policy, args.policy_sha256)
    public_bytes = sign.untrusted_read(args.rom_root, 4096)
    key = sign.rsa_public(serialization.load_pem_public_key(public_bytes))
    canonical = key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    need(public_bytes == canonical and key.public_numbers().e == 65537, 'rom-public-key')
    spki = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    pinned(spki, args.rom_root_sha256)
    need(key.public_numbers() != application_key.public_numbers(), 'rom-application-key-role-reuse')
    # Espressif's ROM digest covers its 776-byte RSA hardware representation,
    # not PKCS#1 DER (application identity) or SPKI (operator root pin).
    rom_digest = espsecure._digest_sbv2_public_key(io.BytesIO(public_bytes))
    need(len(rom_digest) == 32, 'rom-digest')
    build_bytes = pinned(sign.untrusted_read(args.boot_build, 128*1024), args.boot_build_sha256)
    build = document(build_bytes)
    binary = sign.untrusted_read(args.bootloader, plan['configuration']['boot']['bootloader_bytes'])
    need(type(build) is dict and type(build.get('schema')) is int and build['schema'] == 1
         and build.get('scope') == 'esp32s3-cross-build-only' and build.get('platform') == args.platform
         and build.get('cross_build_qualified') is True
         and all(build.get(k) is False for k in ('deployment_permitted', 'physical_qualified', 'rom_authenticated', 'protected_device_counter')),
         'boot-build-scope')
    need(build.get('public_policy_sha256') == target.digest(policy)
         and build.get('public_key_pkcs1_sha256') == target.digest(application_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.PKCS1))
         and build.get('configuration_sha256') == plan['settings']['configuration_sha256']
         and type(build.get('binary_bytes')) is int and 0 < len(binary) == build['binary_bytes']
         and build.get('binary_sha256') == target.digest(binary), 'boot-build-binding')
    observations = assess(summary, args.expected_mac, rom_digest)
    need(inputs == source_inputs(), 'source-changed')
    return dict(observations, schema=1, scope='offline-esp32s3-manufacturing-review',
        tool_inputs=inputs,
        assessed_at=datetime.now(timezone.utc).isoformat(), platform=args.platform,
        summary_sha256=target.digest(summary), boot_build_sha256=target.digest(build_bytes),
        bootloader_sha256=target.digest(binary), policy_sha256=target.digest(policy),
        configuration_sha256=plan['settings']['configuration_sha256'],
        rom_root_spki_sha256=target.digest(spki), rom_root_efuse_digest=rom_digest.hex(),
        esptool_version='5.6.0', summary_schema_sha256=SCHEMA_SHA256,
        rom_signed_bootloader_verified=False, build_provenance_authenticated=False, capture_authenticity_verified=False,
        capture_freshness_verified=False, coding_errors_checked=False,
        hardware_counter_provider_qualified=False, flash_key_custody_qualified=False,
        physical_qualified=False, deployment_permitted=False, device_accessed=False,
        irreversible_actions_performed=False)


def run(args):
    report = make_report(args)
    output = Path(args.out)
    with sign.directory(output.parent) as fd:
        sign.write_new(fd, output.name, target.canonical(report) + b'\n')
        os.fsync(fd)
    return report


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'Preflight invocation refused\n')


def main():
    parser = Parser(description=__doc__)
    for name in ('summary', 'policy', 'rom-root', 'boot-build', 'bootloader', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('summary-sha256', 'policy-sha256', 'rom-root-sha256', 'boot-build-sha256', 'expected-mac'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--platform', required=True)
    args = parser.parse_args()
    try:
        run(args)
    except Exception as error:
        category = str(error) if isinstance(error, Refusal) else 'input-or-runtime'
        print('Preflight refused: ' + category, file=sys.stderr)
        return 1
    print('Offline manufacturing assessment recorded; deployment remains unqualified')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
