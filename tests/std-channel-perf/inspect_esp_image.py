#!/usr/bin/env python3
"""Read-only ESP32-S3 simple-boot image/ROM-warning attribution.

This does not modify flash images, waive a boot warning, authenticate a device,
verify secure boot, or qualify timing. See esptool v4.8.1 bin_image.py:
ESP32FirmwareImage.save(ram_only_header=True).
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct

MAX_IMAGE_BYTES = 64 * 1024 * 1024
WARNING = re.compile(
    r'SHA-256 comparison failed:\s*Calculated:\s*([0-9a-fA-F]{64})'
    r'\s*Expected:\s*([0-9a-fA-F]{64})\s*Attempting to boot anyway\.\.\.')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def segment(data, offset):
    require(offset + 8 <= len(data), 'truncated segment header')
    address, size = struct.unpack_from('<II', data, offset)
    end = offset + 8 + size
    require(size > 0 and size % 4 == 0 and end <= len(data),
            'invalid or truncated segment payload')
    return address, size, end


def parse_image(data):
    """Check supported SIMPLE_BOOT layouts without claiming device execution."""
    require(24 <= len(data) <= MAX_IMAGE_BYTES, 'invalid image length')
    require(data[0] == 0xe9 and struct.unpack_from('<H', data, 12)[0] == 9,
            'not an ESP32-S3 image at offset zero')
    count = data[1]
    require(1 <= count <= 16, 'invalid visible segment count')
    require(data[23] in (0, 1), 'invalid appended digest flag')
    has_digest = data[23] == 1
    offset, checksum, visible = 24, 0xef, []
    for _ in range(count):
        address, size, end = segment(data, offset)
        require((0x3fc88000 <= address < 0x3fd00000 and address + size <= 0x3fd00000)
                or (0x40370000 <= address < 0x403f0000 and address + size <= 0x403f0000)
                or (0x50000000 <= address < 0x50002000 and address + size <= 0x50002000),
                'visible segment is outside supported ESP32-S3 RAM regions')
        for value in data[offset + 8:end]:
            checksum ^= value
        visible.append({'header_offset': offset, 'load_address': address, 'bytes': size})
        offset = end
    # esptool aligns the checksum byte to the last byte of a 16-byte block,
    # including a new block when the last payload already ended on alignment.
    checksum_end = (offset + 16) // 16 * 16
    require(checksum_end + 32 <= len(data), 'truncated checksum or following data')
    require(not any(data[offset:checksum_end - 1]), 'nonzero checksum padding')
    require(data[checksum_end - 1] == checksum, 'RAM payload checksum mismatch')
    calculated = digest(data[:checksum_end])
    following = data[checksum_end:checksum_end + 32].hex()

    # A declared digest precedes the hidden segment chain. The legacy layout
    # places its first padding header directly at checksum_end.
    hidden = []
    if has_digest:
        require(following == calculated, 'declared RAM-prefix digest mismatch')
    offset = checksum_end + (32 if has_digest else 0)
    while offset < len(data):
        if data[offset] == 0xff:
            require(all(value == 0xff for value in data[offset:]),
                    'non-erased data after hidden segment chain')
            break
        require(len(hidden) < 16, 'too many hidden segments')
        address, size, end = segment(data, offset)
        if address == 0:
            require(not any(data[offset + 8:end]), 'nonzero hidden padding')
            kind = 'padding'
        else:
            require((0x42000000 <= address < 0x44000000 and address + size <= 0x44000000)
                    or (0x3c000000 <= address < 0x3e000000 and address + size <= 0x3e000000),
                    'unsupported hidden mapped address')
            require((offset + 8) % 65536 == address % 65536,
                    'hidden flash mapping alignment mismatch')
            kind = 'mapped'
        hidden.append({'header_offset': offset, 'load_address': address,
                       'bytes': size, 'kind': kind})
        offset = end
    require(hidden and hidden[0]['kind'] == 'padding' and
            any(item['kind'] == 'mapped' for item in hidden),
            'missing expected hidden padding/mapped segments')
    require(all(item['kind'] == ('padding' if i % 2 == 0 else 'mapped')
                for i, item in enumerate(hidden)) and len(hidden) % 2 == 0,
            'unexpected simple-boot hidden segment sequence')

    return {
        'schema': 'std-channel-esp-image-diagnostic-v1',
        'image_sha256': digest(data), 'image_bytes': len(data), 'chip_id': 9,
        'visible_segment_count': count, 'digest_appended_flag': has_digest,
        'visible_segments': visible, 'checksum_byte_verified': True,
        'ram_checksum_end_offset': checksum_end,
        'calculated_ram_prefix_sha256': calculated,
        'bytes_at_rom_expected_digest_offset': following,
        'hidden_segments': hidden,
        'ram_prefix_digest_verified': has_digest,
        'boot_warning_resolved': False, 'physical_timing_qualified': False,
        'limitations': 'Offline byte attribution only; no clean-boot, secure-boot or hardware qualification',
    }


def inspect_image(data, transcript):
    """Reproduce ROM values while retaining the original strict log contract."""
    report = parse_image(data)
    calculated = report['calculated_ram_prefix_sha256']
    following = report['bytes_at_rom_expected_digest_offset']
    occurrences = transcript.count('SHA-256 comparison failed:')
    matches = WARNING.findall(transcript)
    require(occurrences <= 1 and len(matches) == occurrences,
            'malformed or multiple ROM digest warnings')
    require('ESP-ROM:esp32s3-' in transcript, 'missing ESP32-S3 ROM boot identity')
    if matches:
        observed, expected = (value.lower() for value in matches[0])
        require(observed == calculated, 'ROM calculated digest differs from supplied image')
        require(expected == following, 'ROM expected bytes differ from supplied image')
    report.update(
        rom_warning_observed=bool(matches),
        rom_warning_matches_image_layout=bool(matches),
        diagnosis=('ROM compared RAM prefix with hidden padding, not a stored digest'
                   if matches and not report['digest_appended_flag']
                   else 'layout inspected; physical boot is not certified'),
    )
    return report


def diagnose(build_directory, transcript):
    root = Path(build_directory).resolve()
    receipt = json.loads((root / 'build.json').read_text())
    require(receipt.get('schema') == 'std-channel-build-v1' and
            receipt.get('status') == 'complete', 'missing completed build receipt')
    relative = receipt.get('image_file')
    require(isinstance(relative, str) and bool(relative), 'missing image path')
    path = (root / relative).resolve()
    require(path.is_relative_to(root) and path.is_file(), 'image escapes build directory or is missing')
    require(path.stat().st_size <= MAX_IMAGE_BYTES, 'image exceeds diagnostic limit')
    data = path.read_bytes()
    require(digest(data) == receipt.get('image_sha256'), 'image differs from build receipt')
    report = inspect_image(data, transcript)
    report.update(build_id=receipt.get('build_id'), source_revision=receipt.get('source_revision'),
                  transcript_sha256=digest(transcript.encode()))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-directory', type=Path, required=True)
    parser.add_argument('--transcript', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = diagnose(args.build_directory, args.transcript.read_bytes().decode('utf-8'))
    encoded = json.dumps(report, indent=2) + '\n'
    if args.output:
        with args.output.open('x') as output:
            output.write(encoded)
    print(encoded, end='')


if __name__ == '__main__':
    main()
