"""Reject misleading placement conclusions from linked disassembly evidence."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import placement


class LinkedPlacementTests(unittest.TestCase):
    def test_arm_conditional_and_tail_branches_reach_flash(self):
        # The original audit recognized bl/b.w, but missed bne.w and plain b.
        assembly = """
20000100 <mpsc_comparison::engine::worker>:
20000100: f000 f802  bl 20000200 <mq_send>
20000104: 4770       bx lr
20000200 <mq_send>:
20000200: d101       bne.n 20000204 <mq_send+0x4>
20000202: 4770       bx lr
20000204: f040 8000  bne.w 10001000 <queue_allocate>
20000208: e000       b 10002000 <queue_lock>
10001000 <queue_allocate>:
10001000: 4770       bx lr
10002000 <queue_lock>:
10002000: 4770       bx lr
"""
        result = placement.inspect_disassembly(assembly, 'pico2w')
        self.assertEqual({v['name'] for v in result['flash_boundaries']},
                         {'queue_allocate', 'queue_lock'})
        self.assertEqual(result['unresolved_direct_targets'], [])
        self.assertEqual(result['indirect_calls'], [])
        self.assertFalse(result['direct_paths_in_sram'])

    def test_arm_veneer_and_register_call_remain_explicit(self):
        assembly = """
20000100 <mpsc_comparison::engine::worker>:
20000100: f000 f802  bl 20000200 <queue_veneer>
20000104: 4798       blx r3
20000106: 4770       bx lr
20000200 <queue_veneer>:
20000200: f8df f000  ldr.w pc, [pc]
20000204: 10001001   .word 0x10001001
10001000 <queue_native>:
10001000: 4770       bx lr
"""
        result = placement.inspect_disassembly(assembly, 'pico2w')
        self.assertEqual(result['flash_boundaries'][0]['name'], 'queue_native')
        self.assertEqual(len(result['indirect_calls']), 1)
        self.assertEqual(result['indirect_calls'][0]['instruction'], 'blx r3')
        self.assertFalse(result['matched_placement_qualified'])

    def test_xtensa_direct_indirect_and_unknown_targets(self):
        assembly = """
40370100 <mpsc_comparison::engine::worker>:
40370100: 001005  call8 40370200 <mq_send>
40370103: 0008e0  callx8 a8
40370106: 0100c6  j 42001000 <queue_allocate>
40370200 <mq_send>:
40370200: 001005  call8 40001234
40370203: f03d    retw.n
42001000 <queue_allocate>:
42001000: f03d    retw.n
"""
        result = placement.inspect_disassembly(assembly, 'esp32s3')
        self.assertEqual(result['flash_boundaries'][0]['name'], 'queue_allocate')
        self.assertEqual(result['unresolved_direct_targets'],
                         [{'caller': 'mq_send', 'address': '0x40001234', 'memory': 'other'}])
        self.assertEqual(result['indirect_calls'][0]['instruction'], 'callx8 a8')
        self.assertFalse(result['direct_paths_in_sram'])
        self.assertFalse(result['indirect_calls_qualified'])

    def test_native_requires_transport_entry_points(self):
        assembly = """
20000100 <mpsc_comparison::engine::worker>:
20000100: 4770  bx lr
"""
        result = placement.inspect_disassembly(assembly, 'pico2w', 'native')
        self.assertEqual(result['missing_roots'], ['mp_queue_send', 'mp_queue_receive'])
        self.assertFalse(result['direct_paths_in_sram'])

    def test_native_mutex_dependency_cannot_be_excluded_as_std_warmup(self):
        assembly = """
20000100 <mpsc_comparison::engine::worker>:
20000100: f000 f802  bl 20000200 <mp_queue_send>
20000200 <mp_queue_send>:
20000200: f000 f802  bl 10001000 <pthread_mutex_lock>
20000300 <mp_queue_receive>:
20000300: 4770       bx lr
10001000 <pthread_mutex_lock>:
10001000: 4770       bx lr
"""
        result = placement.inspect_disassembly(assembly, 'pico2w', 'native')
        self.assertEqual(result['flash_boundaries'][0]['name'], 'pthread_mutex_lock')
        self.assertNotIn('pthread_mutex_lock', result['excluded_cold'])

    def test_direct_sram_observation_is_not_complete_placement_qualification(self):
        assembly = """
20000100 <mpsc_comparison::engine::worker>:
20000100: 4798  blx r3
20000102: 4770  bx lr
"""
        result = placement.inspect_disassembly(assembly, 'pico2w', 'std')
        self.assertTrue(result['direct_paths_in_sram'])
        self.assertFalse(result['matched_placement_qualified'])
        self.assertFalse(result['physical_timing_qualified'])
        self.assertEqual(len(result['indirect_calls']), 1)

    def test_strict_failure_retains_elf_bound_diagnostic(self):
        assembly = """
10000100 <mpsc_comparison::engine::worker>:
10000100: 4770  bx lr
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            elf, output = root / 'image.elf', root / 'hot-audit.json'
            elf.write_bytes(b'identity-only parser test')
            with patch.object(placement.subprocess, 'check_output', return_value=assembly):
                with self.assertRaisesRegex(ValueError, 'SRAM audit failed'):
                    placement.audit(elf, 'test-', output, board='pico2w', backend='std', strict=True)
                report = json.loads(output.read_text())
                self.assertEqual(report['elf_sha256'], hashlib.sha256(elf.read_bytes()).hexdigest())
                self.assertTrue(report['strict_gate'])
                self.assertFalse(report['direct_paths_in_sram'])
                # Diagnostic native builds preserve the same negative result.
                result = placement.audit(elf, 'test-', output, board='pico2w', backend='native')
                self.assertFalse(result['direct_paths_in_sram'])
                self.assertFalse(result['strict_gate'])


if __name__ == '__main__':
    unittest.main()
