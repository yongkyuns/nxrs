"""Check SDK windowed-ABI entry points in little-endian Xtensa ELF objects.

SDK assembly places labels before ENTRY. Text-section literal insertion can
silently put data at those function addresses. Read object bytes, including
non-executable-marked .iram1 sections which objdump -d omits.
"""
import struct


def check_entries(data):
    if data[:6] != b'\x7fELF\x01\x01' or struct.unpack_from('<H', data, 18)[0] != 94:
        raise ValueError('expected ELF32 little-endian Xtensa object')
    section_offset = struct.unpack_from('<I', data, 32)[0]
    entry_size, count = struct.unpack_from('<HH', data, 46)
    sections = [struct.unpack_from('<IIIIIIIIII', data, section_offset + i * entry_size)
                for i in range(count)]
    checked = 0
    for section in sections:
        if section[1] != 2:  # SHT_SYMTAB
            continue
        strings = sections[section[6]]
        names = data[strings[4]:strings[4] + strings[5]]
        for position in range(section[4], section[4] + section[5], section[9]):
            name, value, _, info, _, index = struct.unpack_from('<IIIBBH', data, position)
            if info != 0x12 or index == 0:  # GLOBAL/FUNC, defined
                continue
            offset = sections[index][4] + value
            instruction = data[offset:offset + 3]
            # Xtensa ENTRY a1, frame_size: low 12 bits are 0x136.
            if len(instruction) != 3 or instruction[0] != 0x36 or instruction[1] & 15 != 1:
                symbol = names[name:].split(b'\0')[0].decode()
                raise ValueError('SDK function does not begin with ENTRY: ' + symbol)
            checked += 1
    return checked
