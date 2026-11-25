"""Inspect Xtensa literal-call candidates without changing a firmware image.

Resolve only values that agree at control-flow joins. Literal words must come
from the bound ELF, not objdump's comments. Frame-local spills are followed;
their non-aliasing and dynamic callback targets still require source/assembly
review. This is evidence for a placement review, never automatic qualification.
"""
import re
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from collections import deque
INS = re.compile(r'^(\s*([0-9a-f]+):\s+([0-9a-f]+)\s+)([.\w]+)\s*(.*)$')
BRANCH = re.compile(
    r'^b(?:all|any|bc|bci|bs|bsi|eq|eqi|eqz|ge|gei|geu|geui|gez|lt|lti|ltu|'
    r'ltui|ltz|ne|nei|nez|nall|none|f|t)(?:\.n)?$')
CALL = re.compile(r'^call(x?)(0|4|8|12)$')
TIMER_ROOTS = (
    'systimer_isr', 'ccompare_isr', 'z_timer_expiration_handler',
    'z_thread_timeout', 'nxsem_timeout', 'xtensa_int_decode',
    'xtensa_irq_dispatch', 'irq_dispatch', 'xtensa_handle_irq_lvl',
    'xtensa_excint1_c', '_xtensa_level1_handler', '_xtensa_context_save',
    '_xtensa_context_restore', '_Level1Vector',
)


def resolve(text, read_word):
    lines = text.splitlines()
    functions = []
    current = None
    for index, line in enumerate(lines):
        f = re.fullmatch('([0-9a-f]+) <(.+)>:', line)
        if f:
            current = (f[2], [])
            functions.append(current)
            continue
        m = INS.match(line)
        if m and current:
            operands = [v.strip() for v in m[5].split(',')]
            current[1].append((index, int(m[2], 16), m[4], operands, m[1]))
    records = []
    for function, items in functions:
        if not items:
            continue
        by_address = {row[1]: i for i, row in enumerate(items)}
        states = {0: {}}
        pending = deque([0])
        while pending:
            i = pending.popleft()
            index, addr, op, args, prefix = items[i]
            before = states[i]
            after = dict(before)
            call = CALL.fullmatch(op)
            if op == 'l32r':
                a = re.match('([0-9a-f]+)', args[1])
                value = read_word(int(a[1], 16)) if a else None
                if value is not None:
                    after[args[0]] = (value, int(a[1], 16))
                else:
                    after.pop(args[0], None)
            elif op in ('mov', 'mov.n') and len(args) == 2:
                if args[1] in before:
                    after[args[0]] = before[args[1]]
                else:
                    after.pop(args[0], None)
            elif op in ('s32i', 's32i.n', 'l32i', 'l32i.n') and len(args) == 3 and (args[1] == 'a1'):
                slot = 'sp+' + args[2]
                source, dest = (args[0], slot) if op.startswith('s') else (slot, args[0])
                if source in before:
                    after[dest] = before[source]
                else:
                    after.pop(dest, None)
            elif call:
                # Windowed calls preserve registers below the rotated window.
                # Frame aliases still require manual review, as documented above.
                rotation = int(call[2])
                after = {r: v for r, v in before.items() if r.startswith('sp+')
                         or (re.fullmatch(r'a\d+', r) and 2 <= int(r[1:]) < rotation)}
            elif not (BRANCH.fullmatch(op) or op in ('j', 'entry') or op.startswith(
                    ('ret', 's8i', 's16i', 's32i', 'wsr', 'rsync', 'memw',
                     'isync', 'dsync', 'esync', 'nop'))):
                after.pop(args[0], None)
            successors = []
            if not (op.startswith('ret') or op in ('j', 'jx', 'ill', 'ill.n')) and i + 1 < len(items):
                successors.append(i + 1)
            if BRANCH.fullmatch(op) or op == 'j':
                target = re.match('(?:0x)?([0-9a-f]+)', args[-1])
                if target and int(target[1], 16) in by_address:
                    successors.append(by_address[int(target[1], 16)])
            for destination in successors:
                old = states.get(destination)
                merged = after if old is None else {r: v for r, v in old.items() if after.get(r) == v}
                if old is None or merged != old:
                    states[destination] = dict(merged)
                    pending.append(destination)
        for i, (index, addr, op, args, prefix) in enumerate(items):
            if not (re.fullmatch('callx(?:0|4|8|12)', op) or op == 'jx'):
                continue
            known = states.get(i, {}).get(args[0])
            if known is None:
                continue
            target, literal = known
            records.append(dict(function=function, instruction=hex(addr),
                                target=hex(target), literal=hex(literal)))
            lines[index] = prefix + ('j' if op == 'jx' else op.replace('x', '')) + ' ' + hex(target)[2:]
    return ('\n'.join(lines), records)


def inspect(elf, objdump, backend):
    from elftools.elf.elffile import ELFFile
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mpsc-comparison'))
    import placement
    with Path(elf).open('rb') as stream:
        image = ELFFile(stream)
        if image['e_machine'] != 'EM_XTENSA' or not image.little_endian:
            raise ValueError('expected a little-endian Xtensa ELF')
        regions = [(s['sh_addr'], s.data()) for s in image.iter_sections()
                   if s['sh_flags'] & 2 and s['sh_type'] != 'SHT_NOBITS']
        symbols = {s['st_value']: s.name
                   for s in image.get_section_by_name('.symtab').iter_symbols() if s.name}

    def word(address):
        for start, data in regions:
            if start <= address and address + 4 <= start + len(data):
                return int.from_bytes(data[address - start:address - start + 4], 'little')
    assembly = subprocess.check_output([str(objdump), '-d', '-C', str(elf)], text=True)
    resolved, calls = resolve(assembly, word)
    original = placement.rootpat
    try:
        # Include timer callbacks registered by these fixtures: a call through
        # a mutable callback table cannot be resolved from a literal alone.
        placement.rootpat = re.compile(original.pattern + '|^(?:' + '|'.join(TIMER_ROOTS) + ')$')
        report = placement.inspect_disassembly(resolved, 'esp32s3', backend)
    finally:
        placement.rootpat = original
    for call in report['unresolved_direct_targets']:
        call['symbol'] = symbols.get(int(call['address'], 16))
    report.update(
        schema='mpsc-xtensa-literal-review-v1',
        elf_sha256=hashlib.sha256(Path(elf).read_bytes()).hexdigest(),
        resolver_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        literal_call_candidates=calls, frame_aliases_qualified=False,
        scope='ELF literal values, windowed-call clobbers and agreeing control-flow joins; '
              'frame aliases, dynamic callbacks, data dereferences and cold exclusions require review')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--elf', type=Path, required=True)
    parser.add_argument('--objdump', type=Path, required=True)
    parser.add_argument('--backend', choices=('std', 'native'), required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        parser.error('use a fresh output path')
    args.out.write_text(json.dumps(inspect(args.elf, args.objdump, args.backend), indent=2) + '\n')
