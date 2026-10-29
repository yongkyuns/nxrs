"""Account for an ESP32-S3 NuttX GNU link map; no device access or dependencies."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re

FLASH = ('.iram0.vectors', '.iram0.text', '.dram0.data', '.flash.text',
         '.flash.rodata', '.rtc.data')
RAM = ('.dram0.bss', '.noinit', '.iram0.text_end', '.rtc_reserved')


def read_map(text):
    sections, entries = {}, []
    current = pending = None
    text = re.sub(r'^(\.\S+)\n\s+(0x[\da-f]+)\s+(0x[\da-f]+)',
                  r'\1 \2 \3', text, flags=re.M)
    for line in text.splitlines():
        output = re.match(r'^(\.\S+)\s+(0x[\da-f]+)\s+(0x[\da-f]+)', line)
        if output:
            current, address, size = output.groups()
            sections[current] = (int(address, 16), int(size, 16))
            pending = None
        item = re.match(r'^ (\.\S+)(?:\s+(0x[\da-f]+)\s+(0x[\da-f]+)\s+(.+))?$', line)
        if item:
            pending = item[1]
            if not item[2]:
                continue
            address, size, owner = item.groups()[1:]
        elif pending:
            item = re.match(r'^\s+(0x[\da-f]+)\s+(0x[\da-f]+)\s+(.+)$', line)
            if not item:
                continue
            address, size, owner = item.groups()
        else:
            continue
        entries.append((current, pending, int(address, 16), int(size, 16), owner))
        pending = None
    return sections, entries


def component(name, owner, vendor_crypto):
    if re.search(r'\.str\d+\.\d+', name):
        return 'Merged strings (shared; ownership unavailable)'
    archive = re.search(r'([^/]+\.a)\(([^)]+)\)', owner)
    if not archive:
        return 'Startup objects'
    library, member = archive.groups()
    if library == 'libarch.a' and member in vendor_crypto:
        return 'Radio SDK crypto (libarch.a)'
    if library == 'libapps.a' and member == 'nxrs_app_1.o':
        # The partial link preserves Rust section names, while the SDK's C
        # objects are combined into these unsuffixed output sections.
        if name in ('.text', '.rodata', '.data', '.bss'):
            return 'Application TLS native objects / ABI'
        root = re.search(r'_ZN\d+([A-Za-z_][A-Za-z_0-9]*?)\d', name)
        if root and root[1] in ('std', 'core', 'alloc'):
            return 'Rust std/core/alloc (named sections)'
        if root:
            return 'Rust ' + root[1] + ' (named sections)'
        return 'Rust application constants / other sections'
    return library


def account(sections, entries, selected, vendor_crypto=frozenset()):
    totals = defaultdict(Counter)
    for section in selected:
        start, size = sections[section]
        end = start + size
        events = defaultdict(list)
        for output, name, address, length, owner in entries:
            if output != section or not length:
                continue
            low, high = max(start, address), min(end, address + length)
            if low < high:
                key = component(name, owner, vendor_crypto)
                events[low].append((key, 1))
                events[high].append((key, -1))
        active, previous = Counter(), start
        for position in sorted({start, end} | events.keys()):
            owners = {key for key, count in active.items() if count}
            merged = 'Merged strings (shared; ownership unavailable)'
            key = (merged if merged in owners else next(iter(owners)) if len(owners) == 1
                   else 'Shared input ranges' if owners else 'Linker fill / unowned')
            totals[key][section] += position - previous
            for owner, delta in events[position]:
                active[owner] += delta
            previous = position
    return {key: dict(value) for key, value in sorted(totals.items()) if sum(value.values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('build', type=Path, help='build-nuttx-std-app.sh output directory')
    args = parser.parse_args()
    files = {name: args.build / name for name in ('nuttx/nuttx.map', 'nuttx/nuttx.bin')}
    sections, entries = read_map(files['nuttx/nuttx.map'].read_text())
    dependency = args.build / 'nuttx/arch/xtensa/src/Make.dep'
    # Use actual configured source dependencies, not guessed object names.
    sources = re.findall(r'^(\S+\.o):\s+(\S+)', dependency.read_text().replace('\\\n', ' '), re.M)
    vendor_crypto = {member for member, source in sources if '/components/mbedtls/' in source}
    if not vendor_crypto:
        raise ValueError('configured vendor crypto dependencies are missing')
    counts = account(sections, entries, FLASH + RAM, vendor_crypto)
    files['nuttx/arch/xtensa/src/Make.dep'] = dependency
    payload = sum(sections[name][1] for name in FLASH)
    binary = files['nuttx/nuttx.bin'].stat().st_size
    if binary < payload:
        raise ValueError('image is smaller than selected loadable sections')
    print(json.dumps(dict(
        flash_binary_bytes=binary, loadable_section_bytes=payload,
        image_layout_overhead_bytes=binary - payload,
        sections={name: sections[name][1] for name in FLASH + RAM},
        components=counts,
        radio_crypto_objects=sorted(vendor_crypto),
        sha256={name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()},
        limits=['Specific to the ESP32-S3 flat image layout and current Rust partial-link format.',
                'Merged strings and LTO/inlining prevent exact source ownership; ranges counted once.',
                'Component ownership is retained bytes, not marginal savings from removing a feature.',
                'BSS/noinit consume RAM only; IRAM and initialized data also consume RAM.',
                'Image overhead includes format metadata and alignment; not all is removable.',
                'No heap, task-stack peak or physical execution measurement.']), indent=2))


if __name__ == '__main__':
    main()
