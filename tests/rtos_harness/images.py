"""Pure helpers for parsing and accounting for ESP32-S3 ELF sections."""
import re
from pathlib import Path


def parse_sections(output):
    """Parse GNU/LLVM readelf -W -S rows without depending on column spacing."""
    sections = []
    row = re.compile(r'^\s*\[\s*\d+\]\s+(\S+)\s+(\S+)\s+'
                     r'([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+'
                     r'([0-9a-fA-F]+)\s+\S+\s+(\S+)')
    for line in output.splitlines():
        match = row.match(line)
        if match:
            name, kind, address, offset, size, flags = match.groups()
            sections.append(dict(name=name, type=kind, address=int(address, 16),
                                  offset=int(offset, 16), size=int(size, 16), flags=flags))
    if not sections:
        raise ValueError('readelf produced no parseable ELF section rows')
    return sections


def section_accounting(sections):
    allocated = [s for s in sections if 'A' in s['flags']]
    padding_names = {'.dram0.dummy', '.drom0.dummy',
                     '.flash.text_dummy', '.flash.rodata_dummy'}
    padding = [s for s in allocated if s['name'].lower() in padding_names]
    counted = [s for s in allocated if s['name'].lower() not in padding_names]
    flash = [s for s in counted if s['type'] != 'NOBITS']

    def resident_ram(section):
        address = section['address']
        return (0x40370000 <= address < 0x40400000 or  # ESP32-S3 IRAM
                0x3FC80000 <= address < 0x3FD00000 or  # internal DRAM
                0x50000000 <= address < 0x50100000 or  # RTC slow memory
                0x600FE000 <= address < 0x60100000 or  # RTC fast memory
                0x3D000000 <= address < 0x3E000000)    # external RAM, if linked

    ram = [s for s in counted if s['name'].lower() not in {'.heap', '.heap.noinit'}
           and resident_ram(s)]
    return dict(loadbearing_flash_bytes=sum(s['size'] for s in flash),
                resident_ram_bytes=sum(s['size'] for s in ram),
                flash_sections=[s['name'] for s in flash],
                resident_ram_sections=[s['name'] for s in ram],
                excluded_dummy_padding=[dict(name=s['name'], size=s['size']) for s in padding],
                note=('Complete allocated ELF sections remain listed above. Loadbearing flash '
                      'excludes NOBITS, debug and ESP dummy padding sections; resident RAM counts '
                      'IRAM, DRAM, BSS and noinit sections once, excluding dummy padding and '
                      'unused heap/address gaps. zephyr.bin file length also includes image headers '
                      'and alignment padding, so it can exceed loadbearing section bytes.'))


def esp_idf_section_accounting(sections):
    """Exclude ESP-IDF linker aliases without losing their stored flash bytes."""
    rotext = [s for s in sections if s['name'].lower() == '.rotext_dummy']
    accounted = section_accounting(
        [s for s in sections if s['name'].lower() != '.rotext_dummy'])
    aliases = [s for s in sections if s['name'].lower() == '.rwdata_dummy']
    counted_aliases = [s for s in aliases
                       if s['name'] in accounted['resident_ram_sections']]
    accounted['resident_ram_bytes'] -= sum(s['size'] for s in counted_aliases)
    accounted['resident_ram_sections'] = [
        name for name in accounted['resident_ram_sections']
        if name not in {s['name'] for s in counted_aliases}]
    accounted['excluded_dummy_padding'].extend(
        {'name': s['name'], 'size': s['size']} for s in rotext)
    accounted['excluded_iram_aliases'] = [
        {'name': s['name'], 'size': s['size']} for s in aliases]
    accounted['note'] = (
        'Complete allocated ELF sections remain listed above. Loadbearing flash excludes '
        'NOBITS, debug and ESP dummy padding sections; resident RAM counts IRAM, DRAM, BSS '
        'and noinit sections once, excluding dummy padding, the .rwdata_dummy IRAM alias, '
        'and unused heap/address gaps. embassy.bin length includes the merged image '
        'bootloader and image padding, so it can exceed loadbearing section bytes.')
    return accounted


def image_header(image):
    """Validate the merged ESP image header's mode, frequency and size nibble."""
    data = Path(image).read_bytes() if isinstance(image, (Path, str)) else bytes(image)
    if len(data) < 4:
        raise ValueError('merged image is too short to contain an ESP image header')
    if data[0] != 0xE9:
        raise ValueError(f'ESP image magic must be 0xe9, got 0x{data[0]:02x}')
    mode = data[2]
    if mode != 2:
        raise ValueError(f'ESP image flash mode must be DIO (2), got {mode}')
    size_frequency = data[3]
    if size_frequency & 0x0F != 0:
        raise ValueError(f'ESP image flash frequency must be 40 MHz (0), got {size_frequency & 0x0f}')
    if size_frequency >> 4 != 4:
        raise ValueError(f'ESP image flash size must be 16 MB (4), got {size_frequency >> 4}')
    return {'magic': data[0], 'flash_mode': 'DIO', 'flash_mode_value': mode,
            'flash_frequency_mhz': 40, 'flash_frequency_value': size_frequency & 0x0f,
            'flash_size_mb': 16, 'flash_size_value': size_frequency >> 4}


def assert_stack_section(sections):
    stacks = [section for section in sections if section['name'] == '.stack']
    if len(stacks) != 1 or stacks[0]['size'] != 8192:
        actual = [section['size'] for section in stacks]
        raise ValueError(f'ELF must contain one .stack section of exactly 8192 bytes; got {actual}')
    return stacks[0]
