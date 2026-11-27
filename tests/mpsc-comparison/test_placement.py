"""Reject misleading placement conclusions from linked disassembly evidence."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import placement


class SymbolPlacementTests(unittest.TestCase):
    # Actual v0 spellings from the current Pico CI image, alongside an older
    # legacy spelling. The namespace backreferences are the regression trigger.
    LEGACY_FORCE = '_ZN3std3sys12thread_local3key4racy7LazyKey5force17h0123456789abcdefE'
    V0_FORCE = '_RNvMNtNtNtNtCs2kZSHzTtvYG_3std3sys12thread_local3key4racyNtB2_7LazyKey5force'
    V0_PARK = '_RNvMs_NtNtCs2kZSHzTtvYG_3std6thread6threadNtB4_6Thread4park'
    LEGACY_PARK = '_ZN3std6thread6Thread4park17h0123456789abcdefE'

    @staticmethod
    def object(path, names, literals=False):
        # These stubs test ELF section selection and byte preservation only.
        # No host instructions are executed or used as target timing evidence.
        source = ''.join(f'.section .text.{name},"ax"\n.globl {name}\n{name}:\n.byte 0x12,0x34\n' +
                         (f'.section .literal.{name},"a"\n.globl {name}_literal\n{name}_literal:\n.byte 0x56,0x78\n'
                          if literals else '') for name in names)
        subprocess.run(['cc', '-c', '-x', 'assembler', '-', '-o', str(path)],
                       input=source, text=True, check=True, capture_output=True)

    def test_legacy_and_v0_helpers_move_without_changing_instructions(self):
        names = (self.LEGACY_FORCE, self.V0_FORCE, self.V0_PARK, 'ordinary_helper')
        with tempfile.TemporaryDirectory() as directory:
            obj = Path(directory) / 'helpers.o'
            self.object(obj, names)
            original = obj.read_bytes()
            before = {name: data for _, name, data, _, _ in placement.sections(original)}
            with patch.object(placement, 'CXXFILT', 'c++filt'), patch.object(placement, 'OBJCOPY', 'objcopy'):
                result = placement.transform(obj, 'nuttx')
                after = {name: data for _, name, data, _, _ in placement.sections(obj.read_bytes())}
                self.assertEqual({row['old'] for row in result['sections']},
                                 {'.text.' + name for name in names[:-1]})
                for name in names[:-1]:
                    self.assertEqual(before['.text.' + name], after['.time_critical..text.' + name])
                self.assertEqual(before['.text.ordinary_helper'], after['.text.ordinary_helper'])
                self.assertTrue(result['all_executable_section_bytes_preserved'])
                self.assertIn('<std::thread::thread::Thread>::park',
                              [symbol for row in result['sections'] for symbol in row['symbols']])
                # Rust std helpers are not a Zephyr placement policy.
                obj.write_bytes(original)
                self.assertIsNone(placement.transform(obj, 'zephyr'))
                self.assertEqual(obj.read_bytes(), original)

    def test_unsupported_rust_demangling_fails_before_selection(self):
        with patch.object(placement, 'CXXFILT', 'c++filt'):
            with self.assertRaisesRegex(ValueError, 'could not demangle'):
                placement.rust_names(['_RNnot_a_supported_rust_symbol'])

    def test_known_type_methods_preserve_hot_and_cold_boundaries(self):
        for symbol in (
            '<std::sys::thread_local::key::racy::LazyKey>::force',
            '<std::sys::thread_local::os::LocalPointer>::get',
            '<std::thread::thread::Thread>::park',
            'core::ptr::drop_glue::<std::thread::thread::Thread>',
            'core::ptr::drop_glue::<core::option::Option<std::thread::thread::Thread>>',
            '<alloc::collections::vec_deque::VecDeque<[u32; 16]>>::pop_front',
            '<[u32; 16] as core::cmp::PartialEq>::eq',
            '<usize>::div_ceil',
        ):
            with self.subTest(symbol=symbol):
                self.assertTrue(placement.selected(symbol, 'nuttx'))
                self.assertFalse(placement.cold(symbol))
        for legacy, current in (
            ('std::thread::Thread::new', '<std::thread::thread::Thread>::new'),
            ('std::thread::Thread::new_current', '<std::thread::thread::Thread>::new_current'),
            ('std::thread::Thread::park_timeout', '<std::thread::thread::Thread>::park_timeout'),
            ('std::thread::ThreadId::new', '<std::thread::id::ThreadId>::new'),
            ('std::sys::thread_local::key::racy::LazyKey::lazy_init',
             '<std::sys::thread_local::key::racy::LazyKey>::lazy_init'),
            ('std::sys::thread_local::os::LocalPointer::set',
             '<std::sys::thread_local::os::LocalPointer>::set'),
            ('alloc::collections::vec_deque::VecDeque<T,A>::grow',
             '<alloc::collections::vec_deque::VecDeque<[u32; 16]>>::grow'),
            ('alloc::sync::Arc<T,A>::drop_slow',
             '<alloc::rcs::arc::Arc<std::thread::thread::Inner, std::alloc::System>>::drop_slow'),
        ):
            with self.subTest(legacy=legacy, current=current):
                self.assertTrue(placement.cold(legacy))
                self.assertTrue(placement.cold(current))
        for symbol in (
            'core::ptr::drop_glue::<application::Message>',
            '<application::Thread>::park',
            '<application::LazyKey>::force',
            '<application::VecDeque<u32>>::grow',
            '<application::Arc<u32>>::drop_slow',
            '<alloc::rcs::arc::Arc<u32> as application::Retirement>::drop_slow',
            '<alloc::collections::vec_deque::VecDeque<u32> as application::Growth>::grow',
        ):
            with self.subTest(symbol=symbol):
                self.assertFalse(placement.selected(symbol, 'nuttx'))
                self.assertFalse(placement.cold(symbol))

    def test_esp_qualification_linker_fragments_match_both_thread_symbols(self):
        from elftools.elf.elffile import ELFFile
        root = Path(__file__).resolve().parents[2]
        names = (self.LEGACY_PARK, self.V0_PARK, 'ordinary_helper')
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            obj = directory / 'helpers.o'
            self.object(obj, names, literals=True)
            for name in ('esp32s3-sync.ld', 'esp32s3-mpsc.ld'):
                with self.subTest(fragment=name):
                    fragment = (root / 'platform/nuttx/linker' / name).read_text()
                    script = directory / 'layout.ld'
                    script.write_text('SECTIONS { .iram 0x40370000 : {\n' + fragment +
                                      '\n} .flash 0x42000000 : { *(.text .text.* .literal .literal.*) } }\n')
                    image = directory / 'image.elf'
                    subprocess.run(['ld', '-T', str(script), str(obj), '-o', str(image)],
                                   check=True, capture_output=True)
                    with image.open('rb') as stream:
                        elf = ELFFile(stream)
                        symbols = elf.get_section_by_name('.symtab')
                        for symbol in names[:2]:
                            for suffix in ('', '_literal'):
                                address = symbols.get_symbol_by_name(symbol + suffix)[0]['st_value']
                                self.assertEqual(placement.memory_kind(address, 'esp32s3'), 'internal-code')
                        for suffix in ('', '_literal'):
                            address = symbols.get_symbol_by_name('ordinary_helper' + suffix)[0]['st_value']
                            self.assertEqual(placement.memory_kind(address, 'esp32s3'), 'flash')

    def test_esp_fixture_linker_fragments_match_legacy_and_v0_adapter_owners(self):
        from elftools.elf.elffile import ELFFile
        root = Path(__file__).resolve().parents[2]
        # Legacy/current default compiler encodings from Rust 1.90/1.100
        # representation probes, plus the actual current ESP std send symbol.
        owners = (
            '_ZN108_$LT$mpsc_comparison..transport..Native$LT$_$GT$$u20$as$u20$mpsc_comparison..transport..Channel$LT$_$GT$$GT$4send17h2568c8704be59c4dE',
            '_ZN119_$LT$mpsc_comparison..transport..ordinary_std..Std$LT$_$GT$$u20$as$u20$mpsc_comparison..transport..Channel$LT$_$GT$$GT$4send17h16a493682d330723E',
            '_ZN108_$LT$mpsc_comparison..transport..Native$LT$_$GT$$u20$as$u20$mpsc_comparison..transport..Channel$LT$_$GT$$GT$7receive17h2361fa80b364786dE',
            '_ZN119_$LT$mpsc_comparison..transport..ordinary_std..Std$LT$_$GT$$u20$as$u20$mpsc_comparison..transport..Channel$LT$_$GT$$GT$7receive17h402670f7b454d1ccE',
            '_RNvXNtCshjuDoziolKW_15mpsc_comparison9transportINtB2_6NativeKj4_EINtB2_7ChannelKBX_E4sendB4_',
            '_RNvXs_NtNtCs4zQaGwrTtC4_15mpsc_comparison9transport12ordinary_stdINtB4_3StdKj10_EINtB6_7ChannelKB1c_E4sendB8_',
            '_RNvXNtCshjuDoziolKW_15mpsc_comparison9transportINtB2_6NativeKj4_EINtB2_7ChannelKBX_E7receiveB4_',
            '_RNvXNtNtCshjuDoziolKW_15mpsc_comparison9transport12ordinary_stdINtB2_3StdKj4_EINtB4_7ChannelKB1a_E7receiveB6_',
        )
        # Actual current compiler representations: drop<Std>, Option<Std>'s
        # Clone method, and Std's own Clone method are not Channel hot entries.
        unrelated = (
            '_RINvNtCs8uuN1nrGffB_4core3mem4dropINtNtNtCshjuDoziolKW_15mpsc_comparison9transport12ordinary_std3StdKj4_EEBD_',
            '_RNvXs4_NtCs8uuN1nrGffB_4core6optionINtB5_6OptionINtNtNtCshjuDoziolKW_15mpsc_comparison9transport12ordinary_std3StdKj4_EENtNtB7_5clone5Clone5cloneBR_',
            '_RNvXs_NtNtCshjuDoziolKW_15mpsc_comparison9transport12ordinary_stdINtB4_3StdKj4_ENtNtCs8uuN1nrGffB_4core5clone5Clone5cloneB8_',
            '_RNvXNtCshjuDoziolKW_15mpsc_comparison9transportINtB2_6NativeKj4_EINtB2_7ChannelKBX_E6createB4_',
            '_RNvXNtNtCshjuDoziolKW_15mpsc_comparison9transport12ordinary_stdINtB2_3StdKj4_EINtB4_7ChannelKB1a_E6createB6_',
        )
        fragments = ('tests/nuttx-std/channels/mpsc-fixture-iram.ld',
                     'platform/nuttx/linker/esp32s3-mpsc.ld')
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            obj = directory / 'adapters.o'
            self.object(obj, (*owners, *unrelated, 'ordinary_helper'), literals=True)
            for name in fragments:
                with self.subTest(fragment=name):
                    script = directory / 'layout.ld'
                    script.write_text('SECTIONS { .iram 0x40370000 : {\n' + (root / name).read_text() +
                                      '\n} .flash 0x42000000 : { *(.text .text.* .literal .literal.*) } }\n')
                    image = directory / 'image.elf'
                    subprocess.run(['ld', '-T', str(script), str(obj), '-o', str(image)],
                                   check=True, capture_output=True)
                    with image.open('rb') as stream:
                        elf = ELFFile(stream)
                        symbols = elf.get_section_by_name('.symtab')
                        for symbol in (*owners, *unrelated, 'ordinary_helper'):
                            for suffix in ('', '_literal'):
                                address = symbols.get_symbol_by_name(symbol + suffix)[0]['st_value']
                                self.assertEqual(placement.memory_kind(address, 'esp32s3'),
                                                 'internal-code' if symbol in owners else 'flash')


class LinkedPlacementTests(unittest.TestCase):
    def test_legacy_and_v0_roots_reject_flash_even_without_a_worker_edge(self):
        for name in (
            'std::sync::mpmc::array::SendSlot<T>::write',
            '<std::sync::mpmc::array::SendSlot<[u32; 16]>>::write',
            'std::sync::mpmc::array::RecvSlot<T>::read',
            '<std::sync::mpmc::array::RecvSlot<[u32; 16]>>::read',
            'std::sys::sync::thread_parking::nuttx::Parker::unpark',
            '<std::sys::sync::thread_parking::nuttx::Parker>::unpark',
        ):
            with self.subTest(name=name):
                assembly = f'''\n20000100 <mpsc_comparison::engine::worker>:
20000100: 4770 bx lr
10000100 <{name}>:
10000100: 4770 bx lr
'''
                result = placement.inspect_disassembly(assembly, 'pico2w', 'std')
                self.assertIn(name, [row['name'] for row in result['roots']])
                self.assertEqual([row['name'] for row in result['flash_boundaries']], [name])
                self.assertFalse(result['direct_paths_in_sram'])
                self.assertFalse(result['matched_placement_qualified'])

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

    def test_current_setup_and_abort_boundaries_are_exact(self):
        # These three names come from the retained Pico ELF. Its actual
        # current_or_unnamed branches reach TLS setup or print then abort.
        # Similar names are not evidence that a different call is cold.
        known = (
            'std::sys::thread_local::guard::key::key_based::enable',
            '<std::sys::stdio::unix::Stderr as core::io::write::Write>::write_fmt',
            'core::ptr::drop_glue::<core::result::Result<(), core::io::error::Error>>',
        )
        unknown = (
            'application::' + known[0],
            known[0] + '_other',
            '<std::sys::stdio::unix::Stdout as core::io::write::Write>::write_fmt',
            '<std::sys::stdio::unix::Stderr as application::Write>::write_fmt',
            '<application::Stderr as core::io::write::Write>::write_fmt',
            '<std::sys::stdio::unix::Stderr as core::io::write::Write>::write_all',
            'core::ptr::drop_glue::<core::result::Result<usize, core::io::error::Error>>',
            'core::ptr::drop_glue::<core::result::Result<(), application::Error>>',
            'core::ptr::drop_glue::<std::thread::thread::Thread>',
        )
        for name in (*known, *unknown):
            with self.subTest(name=name):
                assembly = f'''
20000100 <mpsc_comparison::engine::worker>:
20000100: f000 f802  bl 20000200 <boundary_veneer>
20000200 <boundary_veneer>:
20000200: f8df f000  ldr.w pc, [pc]
20000204: 10001001   .word 0x10001001
10001000 <{name}>:
10001000: 4770       bx lr
'''
                result = placement.inspect_disassembly(assembly, 'pico2w', 'std')
                self.assertEqual(result['direct_paths_in_sram'], name in known)
                self.assertEqual(result['excluded_cold'], [name] if name in known else [])
                self.assertEqual([row['name'] for row in result['flash_boundaries']],
                                 [] if name in known else [name])
                self.assertFalse(result['indirect_calls_qualified'])
                self.assertFalse(result['matched_placement_qualified'])
                self.assertFalse(result['physical_timing_qualified'])

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

    def test_xtensa_break_codes_are_not_call_targets(self):
        # These BREAK encodings occur in the actual ESP32-S3 std-channel ELF.
        # Preserve real narrow conditional branches and calls around the trap.
        assembly = """
40370100 <mpsc_comparison::engine::worker>:
40370100: 001005  call8 40370200 <std::sync::mpmc::array::RecvSlot<T>::read>
40370103: f03d    retw.n
40370200 <std::sync::mpmc::array::RecvSlot<T>::read>:
40370200: 111816  beqz a8, 40370208 <std::sync::mpmc::array::RecvSlot<T>::read+0x8>
40370203: 0041f0  break 1, 15
40370206: f02d    break.n 0
40370208: 1b8c    beqz.n a11, 42001000 <native_boundary>
4037020a: 0008e0  callx8 a8
42001000 <native_boundary>:
42001000: f03d    retw.n
"""
        result = placement.inspect_disassembly(assembly, 'esp32s3', 'std')
        self.assertEqual(result['unresolved_direct_targets'], [])
        self.assertEqual(result['flash_boundaries'][0]['name'], 'native_boundary')
        self.assertEqual(result['indirect_calls'][0]['instruction'], 'callx8 a8')
        self.assertFalse(result['matched_placement_qualified'])

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
