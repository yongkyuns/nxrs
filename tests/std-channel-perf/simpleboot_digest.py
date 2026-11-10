"""Opt-in, offset-preserving RAM-prefix digest candidate; never a flash command.

The ROM prefix digest does NOT cover hidden flash-mapped segments or authenticate
firmware. Historical inputs and their receipts are never rewritten. Deployable
candidate generation requires the matching rebuilt loader patch ledger.
"""
import hashlib
import json
from pathlib import Path
import struct

from inspect_esp_image import parse_image, require

PROFILE = 'ram-prefix-digest-v1'
PATCH_NAME = '0107-xtensa-simpleboot-hash-offset.patch'
LOADER_PATH = 'arch/xtensa/src/common/espressif/esp_loader.c'
KERNEL_REVISION = '2b5c0de5e933281c209f954a63b99cf10c132346'
LOADER_AFTER = 'a35533da7d7b800cbac6a6e6de2a307cff727284c0ced2db54269290691c00a9'
LOADER_BEFORE = '7ac941445818043cdb5ce0f1444cb53c954a8d920139d3181c81ad20d0563ddb'
ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / 'upstream/nuttx/patches' / PATCH_NAME
MAX_IMAGE_BYTES = 64 * 1024 * 1024


def sha(data):
    return hashlib.sha256(data).hexdigest()


def repack(data):
    """Pure byte-layout experiment. Does not establish a compatible executable."""
    before = parse_image(data)
    require(not before['digest_appended_flag'], 'input already declares a digest')
    end = before['ram_checksum_end_offset']
    padding = before['hidden_segments'][0]
    # Keep a positive, word-aligned padding payload after taking 32 bytes.
    require(padding['header_offset'] == end and padding['kind'] == 'padding'
            and padding['bytes'] >= 36, 'insufficient first padding for digest')
    result = bytearray(data)
    result[23] = 1
    result[end:end + 32] = hashlib.sha256(result[:end]).digest()
    struct.pack_into('<II', result, end + 32, 0, padding['bytes'] - 32)
    result = bytes(result)
    after = parse_image(result)
    require(len(result) == len(data), 'candidate moved image boundary')
    require(result[:23] == data[:23] and result[24:end] == data[24:end]
            and result[end + 40:] == data[end + 40:], 'candidate changed payload bytes')
    old_mapped = [s for s in before['hidden_segments'] if s['kind'] == 'mapped']
    new_mapped = [s for s in after['hidden_segments'] if s['kind'] == 'mapped']
    require(old_mapped == new_mapped, 'candidate moved flash mappings')
    evidence = {
        'schema': 'std-channel-simpleboot-digest-v1', 'profile': PROFILE,
        'input_sha256': sha(data), 'output_sha256': sha(result),
        'image_bytes': len(result), 'digest_offset': end,
        'ram_prefix_sha256': after['calculated_ram_prefix_sha256'],
        'first_padding_payload_before': padding['bytes'],
        'first_padding_payload_after': padding['bytes'] - 32,
        'mapped_segments': new_mapped, 'mapped_offsets_and_bytes_preserved': True,
        'ram_payloads_preserved': True, 'prefix_digest_verified': True,
        'digest_scope': 'header through aligned ROM-visible RAM checksum only',
        'secure_boot_qualified': False, 'boot_warning_resolved': False,
        'physical_timing_qualified': False,
    }
    return result, evidence


def verify_loader(firmware):
    """Bind candidate to exact private source bytes and the applied patch ledger."""
    firmware = Path(firmware).resolve()
    record = json.loads((firmware / 'nuttx-patches.json').read_text())
    require(record.get('component') == 'nuttx' and
            record.get('upstream_revision') == KERNEL_REVISION, 'wrong loader kernel pin')
    matches = [p for p in record.get('patches', []) if p.get('name') == PATCH_NAME]
    require(len(matches) == 1, 'matching loader patch must be built first')
    entry = matches[0]
    require(entry.get('sha256') == sha(PATCH.read_bytes()), 'loader patch hash mismatch')
    hashes = entry.get('files', {}).get(LOADER_PATH, {})
    source = (firmware / 'nuttx' / LOADER_PATH).resolve()
    require(source.is_relative_to(firmware / 'nuttx') and source.is_file(),
            'loader source missing or outside private build')
    require(hashes.get('before') == LOADER_BEFORE and
            hashes.get('after') == LOADER_AFTER == sha(source.read_bytes()), 'loader source/ledger mismatch')
    # A later patch may not silently undo this one while leaving an old entry.
    require('if (image_header.hash_appended == 1)' in source.read_text() and
            'offset += 32;' in source.read_text(), 'loader digest skip missing')
    return {'patch': PATCH_NAME, 'patch_sha256': entry['sha256'],
            'loader_source_sha256': hashes['after'], 'kernel_revision': KERNEL_REVISION}


def prepare(firmware, input_image):
    """Called only after a successful private firmware build; never edits input."""
    firmware = Path(firmware).resolve()
    input_image = Path(input_image).resolve()
    require(input_image.is_relative_to(firmware / 'nuttx'), 'input outside firmware directory')
    require(input_image.stat().st_size <= MAX_IMAGE_BYTES, 'image exceeds size limit')
    config = (firmware / 'resolved.config').read_text().splitlines()
    require('CONFIG_ESPRESSIF_SIMPLE_BOOT=y' in config and
            'CONFIG_ARCH_CHIP_ESP32S3=y' in config, 'requires ESP32-S3 SIMPLE_BOOT')
    loader = verify_loader(firmware)
    candidate, evidence = repack(input_image.read_bytes())
    output = firmware / 'nuttx/nuttx.rom-digest.bin'
    report = firmware / 'simpleboot-digest.json'
    require(not output.exists() and not report.exists(), 'candidate output already exists')
    evidence.update(loader=loader, source_image=input_image.name,
                    output_image=output.name, qualification='build-candidate-only')
    # Exclusive creation protects historical/other runs; build.json stays failed
    # if either write fails. No pre-existing file is replaced.
    with output.open('xb') as stream:
        stream.write(candidate)
    with report.open('x') as stream:
        json.dump(evidence, stream, indent=2)
        stream.write('\n')
    return output, report
