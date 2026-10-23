"""Shared approval of an exact qualification boot build and independent ROM root."""
from dataclasses import dataclass
import io
import json
import math
import re

from cryptography.hazmat.primitives import serialization
import espsecure

import build_esp32s3 as builder
import sign
import target_plan as target


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


@dataclass(frozen=True)
class ApprovedBoot:
    plan: dict
    policy: bytes
    public: bytes
    key: object
    spki: bytes
    rom_digest: bytes
    build_bytes: bytes
    binary: bytes


def approved_boot(args):
    builder.checked_environment()
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
    build_bytes = pinned(sign.untrusted_read(args.boot_build, 1024*1024), args.boot_build_sha256)
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
    return ApprovedBoot(plan, policy, public_bytes, key, spki, rom_digest, build_bytes, binary)
