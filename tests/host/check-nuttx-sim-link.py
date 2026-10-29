#!/usr/bin/env python3
"""Reject accidental host-POSIX linkage at NuttX's relocation boundary."""
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
mapping = {}
for line in (root / 'nuttx/arch/sim/src/nuttx-names.dat').read_text().splitlines():
    fields = line.split()
    if len(fields) == 2 and not fields[0].startswith('#'):
        mapping[fields[0]] = fields[1]
symbol_lines = [line.split() for line in (root / 'symbols.txt').read_text().splitlines()]
defined = {fields[-1] for fields in symbol_lines if len(fields) >= 3 and fields[-2].upper() != 'U'}
undefined = {fields[-1] for fields in symbol_lines if len(fields) >= 2 and fields[-2].upper() == 'U'}
relocations = (root / 'relocations.txt').read_text()

def target_reference(function):
    # NuttX renames only symbols also used by simulator host support. Other
    # target functions legitimately keep their original names, but must still
    # be DEFINED in nuttx.rel rather than left for the final host libc link.
    symbol = mapping.get(function, function)
    referenced = re.search(r'(?<![A-Za-z0-9_])' + re.escape(symbol) + r'(?![A-Za-z0-9_])', relocations)
    return symbol in defined and symbol not in undefined and referenced

for function in ['open', 'read', 'write', 'lseek', 'ftruncate', 'fsync', 'socket', 'sendto', 'recvfrom',
                 'pthread_create', 'clock_gettime', 'getrandom']:
    renamed = mapping.get(function)
    assert renamed and renamed != function, f'missing NuttX isolation rename: {function}'
    assert target_reference(function), f'no defined in-image target/call: {renamed}'
    assert function not in undefined, f'host syscall escaped NuttX: {function}'
    print(f'PASS target link: {function} -> {renamed}')

# send/recv may lower to sendto/recvfrom in the actual target headers. Do not
# invent rename entries or require a non-existent out-of-line wrapper symbol.
for purpose, alternatives in {
    'send': ['send', 'sendto'],
    'recv': ['recv', 'recvfrom'],
    'pthread_join': ['pthread_join', 'pthread_timedjoin_np'],
    'sem_wait': ['sem_wait'],
    'nanosleep': ['nanosleep', 'clock_nanosleep'],
    'random port bytes': ['arc4random_buf'],
}.items():
    resolved = [name for name in alternatives if target_reference(name)]
    assert resolved, f'no target implementation/call for {purpose}: {alternatives}'
    assert not any(name in undefined for name in alternatives), f'host dependency for {purpose}'
    print(f'PASS target call: {purpose} -> {resolved}')

for function in ['rc_rust_run', 'rc_nx_camera_open', 'rc_nx_read', 'rc_nx_close_real', 'rc_nx_file_create_real', 'rc_nx_append', 'rc_nx_udp_open', 'rc_nx_send']:
    assert function in defined, f'missing application/bridge code in nuttx.rel: {function}'
assert 'host_random' in undefined, 'missing explicit simulator host entropy boundary'
print('PASS: Rust application and HAL bridge are linked inside nuttx.rel')
