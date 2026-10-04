"""Compile a pinned target layout ledger without linking or flashing firmware."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

FIELDS = ('message_header', 'event_request', 'ack16_request', 'ack28_request',
          'allocator_node', 'allocator_overhead', 'allocator_alignment', 'allocator_minimum',
          'queue_metadata', 'inode_base', 'file_entry', 'poll_descriptor',
          'general_preallocated_messages', 'irq_preallocated_messages',
          'sysv_buffer', 'sysv_enabled')


def parse_layout(assembly):
    match = re.search(r'^nxrs_transport_resource_layout:\n(.*?)(?=^\s*\.ident|\Z)',
                      assembly, re.M | re.S)
    if not match: raise ValueError('missing target layout symbol')
    values = re.findall(r'^\s*\.word\s+(\d+)\s*$', match[1], re.M)
    if len(values) != len(FIELDS): raise ValueError('incomplete target layout')
    return dict(zip(FIELDS, map(int, values)))


def nominal_allocation(request, layout):
    alignment = layout['allocator_alignment']
    if alignment == 0 or alignment & (alignment - 1): raise ValueError('invalid heap alignment')
    size = max(request + layout['allocator_overhead'], layout['allocator_minimum'])
    return (size + alignment - 1) & -alignment


def message_ledger(layout):
    event = nominal_allocation(layout['event_request'], layout)
    result = {}
    for bytes in (16, 28):
        ack = nominal_allocation(layout[f'ack{bytes}_request'], layout)
        result[str(bytes)] = dict(event_nominal_block=event, ack_nominal_block=ack,
                                 queued_slots_nominal=45 * event + 60 * ack,
                                 active_calls_nominal=4 * event + 15 * max(event, ack) + ack,
                                 slots_plus_active_nominal=45 * event + 60 * ack +
                                     4 * event + 15 * max(event, ack) + ack)
    result['queue_metadata_nominal'] = 60 * nominal_allocation(layout['queue_metadata'], layout)
    # mq_initialize.c aligns fixed pools to sizeof(void*) (4 on this target),
    # unlike the 8-byte dynamic heap alignment. This is already in kernel BSS.
    result['posix_fixed_pool_bss'] = ((layout['event_request'] + 3) & -4) * (
        layout['general_preallocated_messages'] + layout['irq_preallocated_messages'])
    result['sysv_fixed_pool_bss'] = layout['sysv_buffer'] * layout['general_preallocated_messages'] * layout['sysv_enabled']
    result['fixed_pool_bss'] = result['posix_fixed_pool_bss'] + result['sysv_fixed_pool_bss']
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nuttx', type=Path, required=True)
    parser.add_argument('--prefix', default='xtensa-esp32s3-elf-')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists(): parser.error('fresh output required')
    nuttx = args.nuttx.resolve()
    source = Path(__file__).with_name('transport_resource_layout.c')
    command = [args.prefix + 'gcc', '-isystem', str(nuttx / 'include'),
               '-I', str(nuttx / 'sched'), '-I', str(nuttx / 'mm'),
               '-D__NuttX__', '-D__KERNEL__', '-Os', '-S', str(source), '-o', '-']
    assembly = subprocess.check_output(command, text=True)
    layout = parse_layout(assembly)
    paths = [source, nuttx / '.config', nuttx / 'mm/mm_heap/mm.h',
             nuttx / 'mm/mm_heap/mm_malloc.c', nuttx / 'include/nuttx/mqueue.h',
             nuttx / 'sched/mqueue/mqueue.h', nuttx / 'sched/mqueue/mq_send.c',
             nuttx / 'sched/mqueue/msg.h',
             nuttx / 'sched/mqueue/mq_receive.c', nuttx / 'sched/mqueue/mq_initialize.c']
    report = dict(schema=1, target='32-bit ESP32-S3 NuttX', compiler_command=command,
                  assembly=assembly, layout=layout, message_ledger=message_ledger(layout),
                  source_sha256={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
                  limits=['Nominal rounded blocks, not an allocation trace or measured peak.',
                          'Unsplit heap tails, fragmentation, inodes/names, descriptor tables, '
                          'poll waits, app state and thread resources are additional.',
                          'Active-call allowance covers each producer, worker or collector once; '
                          'workers cannot send and receive simultaneously.',
                          'General fixed-pool use reduces dynamic allocations; no pool saving '
                          'is assumed in nominal message totals.',
                          'Fixed pool is already part of kernel BSS; do not add it twice.'])
    args.out.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__': main()
