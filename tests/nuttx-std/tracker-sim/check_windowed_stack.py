"""Check for arithmetic stack-pointer updates in the linked Xtensa Rust code."""

import argparse
from pathlib import Path
import re
import subprocess
import tempfile


SYMBOL = re.compile(r'^[0-9a-f]+ <(.+)>:$')
ENTRY = re.compile(r'^\s*[0-9a-f]+:\s+[0-9a-f]+\s+entry\s+a1,')
SP_ARITHMETIC = re.compile(
    r'^\s*[0-9a-f]+:\s+[0-9a-f]+\s+(?:add(?:i|mi)?|sub|and)(?:\.n)?\s+a1,'
)


def check_windowed_stack(disassembly):
    # ENTRY proves the function uses the windowed ABI. Arithmetic writes to a1
    # bypass MOVSP's register-window spill handling.
    rust_symbol = None
    windowed = False
    found_tracker = False
    for line in disassembly.splitlines():
        symbol = SYMBOL.fullmatch(line)
        if symbol:
            rust_symbol = symbol[1] if '::' in symbol[1] else None
            windowed = False
        elif rust_symbol and ENTRY.match(line):
            windowed = True
            found_tracker |= rust_symbol == 'nxrs_motion_tracker::runtime::track'
        elif windowed and SP_ARITHMETIC.match(line):
            raise ValueError(f'{rust_symbol} changes a1 without MOVSP: {line.strip()}')
    if not found_tracker:
        raise ValueError('Motion Tracker windowed entry was absent from the disassembly')


def disassemble(elf, objdump, objcopy):
    # Linked .xt.prop records can misclassify Rust function bodies as data,
    # hiding their instructions even with objdump -D. Preserve symbols and
    # literal ranges, but ignore these records in a disposable copy.
    with tempfile.TemporaryDirectory() as directory:
        checked = Path(directory) / 'firmware.elf'
        subprocess.check_call([objcopy, '--remove-section=.xt.prop', elf, checked])
        return subprocess.check_output([objdump, '-d', '-C', checked], text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('elf', type=Path)
    parser.add_argument('--objdump', default='xtensa-esp32s3-elf-objdump')
    parser.add_argument('--objcopy', default='xtensa-esp32s3-elf-objcopy')
    args = parser.parse_args()
    disassembly = disassemble(args.elf, args.objdump, args.objcopy)
    try:
        check_windowed_stack(disassembly)
    except ValueError as error:
        parser.exit(1, f'{error}\n')
    print('PASS: linked windowed Rust functions use no arithmetic stack-pointer updates')


if __name__ == '__main__':
    main()
