"""The execution oracle must refuse missing, reordered or fabricated evidence."""
import time
import unittest
import tempfile
from pathlib import Path

import qualify_admission
import sign
import target_plan
import boot_execution as execution
import boot_resources as resources
import qualify_watchdog_handoff as handoff


class Connection:
    def __init__(self, raw):
        self.raw, self.sent = raw, []

    def settimeout(self, _):
        pass

    def recv(self, size):
        # Deliberately fragment every multi-byte read.
        data, self.raw = self.raw[:1], self.raw[1:]
        return data

    def sendall(self, data):
        self.sent.append(data)


class Protocol(unittest.TestCase):
    def remote(self, raw):
        return execution.Remote(Connection(raw), time.monotonic() + 10)

    def test_fragmented_ack_packet_and_little_endian_word(self):
        raw = b'bc463b40'
        remote = self.remote(b'+$' + raw + b'#' + f'{sum(raw)%256:02x}'.encode())
        self.assertEqual(remote.register(0), 0x403b46bc)

    def test_bad_checksum_nack_truncation_and_error_are_refused(self):
        for raw in (b'+$bc463b40#00', b'-$OK#9a', b'+$0004', b'+$E01#a6'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.remote(raw).register(0)

    def test_escaped_and_run_length_responses(self):
        self.assertEqual(execution.Remote.decode(b'0* '), b'0000')
        self.assertEqual(execution.Remote.decode(b'}\x03'), b'#')
        for raw in (b'* ', b'}', b'0*', b'0*\x1f'):
            with self.assertRaises(ValueError):
                execution.Remote.decode(raw)


class Oracle(unittest.TestCase):
    def positive(self):
        kinds = ['watchdog-init', 'watchdog-hal-init', 'boot-go', 'hook', 'watchdog-progress', 'hook-return', 'stock-check', 'stock-signature', 'entry', 'park']
        events = [{'kind': kind, 'pc': index} for index, kind in enumerate(kinds)]
        next(event for event in events if event['kind'] == 'hook-return')['value'] = 7
        events[-2]['marker'], events[-1]['marker'] = 0x11223344, 0x4e585253
        return events

    def test_complete_signed_application_and_signature_refusal(self):
        execution.check_observation('offline-signer-positive', self.positive(), 7)
        events = self.positive()[:-2] + [{'kind': 'no-image'}]
        execution.check_observation('signature-corruption', events, 7)

    def test_missing_or_duplicated_required_execution(self):
        positive = self.positive()
        for index in range(len(positive)):
            with self.subTest(index=index), self.assertRaises((ValueError, StopIteration)):
                execution.check_observation('offline-signer-positive', positive[:index] + positive[index+1:], 7)
        with self.assertRaises(ValueError):
            execution.check_observation('offline-signer-positive', positive[:1] + positive, 7)

    def test_wrong_policy_must_not_continue_stock_verification(self):
        execution.check_observation('signed-wrong-target', self.positive()[:5] + [{'kind': 'hook-return', 'value': 8}, {'kind': 'no-image'}], 7)
        with self.assertRaises(ValueError):
            execution.check_observation('signed-wrong-target', self.positive()[:-2] + [{'kind': 'no-image'}], 7)

    def test_marker_initialization_and_actual_write_are_required(self):
        for index in (-1, -2):
            events = self.positive()
            events[index]['marker'] ^= 1
            with self.assertRaises(ValueError):
                execution.check_observation('offline-signer-positive', events, 7)

    def test_entry_alone_or_reordered_signature_is_insufficient(self):
        events = self.positive()
        events[-3], events[-2] = events[-2], events[-3]
        with self.assertRaises(ValueError):
            execution.check_observation('offline-signer-positive', events, 7)
        with self.assertRaises(ValueError):
            execution.check_observation('offline-signer-positive', self.positive()[:-1] + [{'kind': 'no-image'}], 7)

    def test_unprotected_target_floor_is_explicitly_skipped(self):
        self.assertEqual(execution.SKIP, {'stored-security-floor': 'no-protected-target-counter-backend'})
        self.assertNotIn('stored-security-floor', execution.ACCEPT)
        self.assertIn('maximum-size-positive', execution.ACCEPT)

    def test_unrelated_local_symbol_duplicates_do_not_hide_global_sites(self):
        table = execution.symbol_table("40378000 00000020 T fixture_entry\n3fc90000 00000004 D NXRS_ENTRY_WITNESS\n00000001 00000004 t local\n00000002 00000004 t local")
        self.assertEqual(set(table), {'fixture_entry', 'NXRS_ENTRY_WITNESS'})
        with self.assertRaises(ValueError):
            execution.symbol_table("40378000 00000020 T fixture_entry\n40378040 00000020 T fixture_entry")

    def test_no_image_loop_must_follow_unique_boot_go_call(self):
        main = [(1, 'j', '1 <initial>'), (3, 'call8', '10 <boot_go>'), (5, 'j', '5 <failure>'), (7, 'j', '7 <returned>')]
        self.assertEqual(execution.fail_loop(main), 5)
        for changed in (main[:-1], main + [(9, 'call8', '10 <boot_go>')]):
            with self.assertRaises(ValueError):
                execution.fail_loop(changed)

    def test_target_register_map_cannot_silently_change(self):
        text = '\n'.join('XTREG(%d,0,32,4,4,0,0,0,%d,0,%s,0)' % (num, kind, name)
                         for name, num, kind in [('pc',0,9), ('ar0',1,1), ('ar63',64,1), ('windowbase',69,2)])
        self.assertEqual(execution.register_mapping(text), {'pc':0,'windowbase':69,'ar0':1})
        with self.assertRaises(ValueError):
            execution.register_mapping(text.replace('windowbase','other'))


class SignedFixtures(unittest.TestCase):
    def test_executable_body_reuses_only_the_matching_in_memory_authority(self):
        settings = target_plan.plan('esp32s3-camera-mcuboot')['settings']
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            authority = root / 'authority'
            authority.mkdir(mode=0o700)
            password = b'fixture-only-memory-password-0123456789'
            pin = sign.initialize(authority, settings, password)
            with sign.directory(authority) as fd:
                public, policy, key, _ = sign.state_open(fd, password, pin)
            signing = (public, policy, key)
            images = root / 'images'
            images.mkdir(mode=0o700)
            body = b'actual-executable-fixture-body'
            _, labels, _ = qualify_admission.fixtures(images, settings=settings, body=body, signing=signing)
            self.assertEqual(len(labels), 31)
            raw = (images / 'offline-signer-positive.bin').read_bytes()
            self.assertEqual(raw[32:32+len(body)], body)
            maximum = resources.maximum_fixture(body, signing, settings, pin, images)
            self.assertEqual(maximum['container_bytes'] + maximum['reserved_trailer_bytes'], settings['layout']['slot_bytes'])
            self.assertTrue(maximum['one_byte_over_budget_refused'])
            sign.verify_image(raw, public, pin, settings['target'], settings['configuration_sha256'], 1)
            bad = dict(settings, target='other-camera')
            with self.assertRaises(ValueError):
                qualify_admission.fixtures(images, settings=bad, body=body, signing=signing)
            with self.assertRaises(ValueError):
                qualify_admission.fixtures(images, settings=settings, body=b'', signing=signing)


class ResourceControls(unittest.TestCase):
    def defines(self):
        return "#define CONFIG_ESP_MCUBOOT_WDT_ENABLE 1\n#define CONFIG_BOOTLOADER_WDT_ENABLE 1\n#define CONFIG_BOOTLOADER_WDT_TIME_MS 9000\n"

    def test_product_flag_cannot_substitute_for_sdk_enablement(self):
        resources.watchdog_defines(self.defines())
        for text in (self.defines().replace('#define CONFIG_BOOTLOADER_WDT_ENABLE 1\n', ''),
                     self.defines().replace('ENABLE 1', 'ENABLE 0', 1),
                     self.defines().replace('9000', '0'), self.defines().replace('9000','90000')):
            with self.assertRaises(ValueError):
                resources.watchdog_defines(text)

    def test_stack_write_highwater_requires_intact_guard_and_real_changes(self):
        paint = bytes([0xa5]) * 8000
        observed = paint[:1500] + bytes(6500)
        result = resources.stack_observation(paint, observed, 4096, 4096+8192, 4096+8064)
        self.assertEqual(result['untouched_guard_bytes'], 1500)
        self.assertEqual(result['observed_stack_write_bytes_from_top'], 6692)
        self.assertFalse(result['full_startup_or_static_bound_qualified'])
        with self.assertRaises(resources.StackGuardError):
            resources.stack_observation(paint, bytes([0])+observed[1:], 4096, 4096+8192, 4096+8064)
        for bad in (paint, observed[:-1]):
            with self.assertRaises(ValueError):
                resources.stack_observation(paint, bad, 4096, 4096+8192, 4096+8064)

    def test_actual_handoff_extraction_refuses_missing_or_multiple_functions(self):
        function = 'void esp32s3_wdt_early_deinit(void) { native_operation(); }'
        self.assertEqual(handoff.early_function('other\n' + function), function+'\n')
        for text in ('', function + '\n' + function, function.replace('native_operation();', 'if (flag) { nested(); }')):
            with self.assertRaises(ValueError):
                handoff.early_function(text)

    def test_absolute_stack_boundary_symbol_must_be_unique(self):
        self.assertEqual(execution.absolute_symbol('3fcd7a4c A _dram_end','_dram_end'), 0x3fcd7a4c)
        for text in ('', '3fcd7a4c T _dram_end', '3fcd7a4c A _dram_end\n3fcd7a4c A _dram_end'):
            with self.assertRaises(ValueError):
                execution.absolute_symbol(text, '_dram_end')


if __name__ == '__main__':
    unittest.main()
