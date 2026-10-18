"""Fixture UUID selection across advertising reports, without any radios."""
import importlib.util
from pathlib import Path
import types
import unittest
import re
import uuid
from unittest.mock import AsyncMock, Mock, patch

path = Path(__file__).resolve().parents[1] / 'nuttx-std/radio/run.py'
spec = importlib.util.spec_from_file_location('radio_discovery', path)
radio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(radio)


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def discover(self, events, failure=None, observer=None, setup_seconds=0):
        calls = []

        class Scanner:
            def __init__(self, **kwargs):
                self.callback = kwargs['detection_callback']
                calls.append(kwargs)

            async def __aenter__(self):
                calls.append('entered')
                for device, uuids, *names in events:
                    self.callback(device, types.SimpleNamespace(
                        service_uuids=uuids, local_name=names[0] if names else None))
                return self

            async def __aexit__(self, *args):
                calls.append('closed')

        sleep = AsyncMock(side_effect=failure)
        def started(deadline):
            self.assertIn('entered', calls)
            self.assertNotIn('closed', calls)
            observer(deadline)

        with patch.dict('sys.modules', {'bleak': types.SimpleNamespace(BleakScanner=Scanner)}), \
             patch.object(radio.asyncio, 'sleep', sleep), \
             patch.object(radio, 'time', types.SimpleNamespace(
                 monotonic=Mock(side_effect=[100, 100 + setup_seconds]))):
            try:
                return await radio.discover_fixture(scan_started=started if observer else None)
            finally:
                sleep.assert_awaited_once_with(max(0, 5 - setup_seconds) if observer else 5)
                self.assertEqual(set(calls[0]), {'detection_callback'})
                self.assertEqual(calls[-1], 'closed')

    async def test_match_survives_name_only_report_and_duplicate_identity(self):
        own = types.SimpleNamespace(address='private-fixture', name='fixture')
        other = types.SimpleNamespace(address='private-other', name='nxrs-gatt')
        result = await self.discover([(other, []), (own, [radio.SERVICE.upper()]),
                                      (own, []), (own, [radio.SERVICE])])
        self.assertIs(result, own)

    async def test_no_match_and_multiple_matches_are_failures_without_identifiers(self):
        for count in (0, 2):
            events = [(types.SimpleNamespace(address=f'private-{index}', name=None), [radio.SERVICE])
                      for index in range(count)]
            with self.assertRaisesRegex(RuntimeError, f'^expected one fixture advertiser, found {count}$'):
                await self.discover(events)

    async def test_failed_scan_closes_context_and_never_returns_a_match(self):
        own = types.SimpleNamespace(address='private-fixture', name=None)
        with self.assertRaises(TimeoutError):
            await self.discover([(own, [radio.SERVICE])], failure=TimeoutError())

    async def test_observer_starts_inside_scan_and_cannot_extend_its_window(self):
        own = types.SimpleNamespace(address='private-fixture', name=None)
        for seconds in (0, 1.25, 8):
            with self.subTest(setup_seconds=seconds):
                observer = Mock()
                self.assertIs(await self.discover([(own, [radio.SERVICE])],
                                                  observer=observer, setup_seconds=seconds), own)
                observer.assert_called_once_with(105)

    async def test_name_only_reports_do_not_rescue_missing_service_uuid(self):
        own = types.SimpleNamespace(address='private-fixture', name=radio.FIXTURE_NAME)
        other = types.SimpleNamespace(address='private-other', name='private-other-name')
        events = [(own, [], radio.FIXTURE_NAME), (own, []), (other, [], 'private-other-name')]
        with patch('builtins.print') as output:
            with self.assertRaisesRegex(RuntimeError, '^expected one fixture advertiser, found 0$'):
                await self.discover(events)
        output.assert_called_once_with(
            'NXRS_HOST_GATT_DISCOVERY service_reports=0 named_reports=1 named_without_service=1 '
            'os_name_reports=2 os_name_without_service=2 matched_devices=0', flush=True)

    async def test_success_stays_quiet_and_does_not_require_a_name(self):
        own = types.SimpleNamespace(address='private-fixture', name=None)
        with patch('builtins.print') as output:
            self.assertIs(await self.discover([(own, [radio.SERVICE])]), own)
        output.assert_not_called()

    async def test_ambiguous_matches_fail_with_only_numeric_counts(self):
        own = types.SimpleNamespace(address='private-fixture', name=radio.FIXTURE_NAME)
        other = types.SimpleNamespace(address='private-other', name='private-other-name')
        with patch('builtins.print') as output:
            with self.assertRaisesRegex(RuntimeError, '^expected one fixture advertiser, found 2$'):
                await self.discover([(own, [radio.SERVICE], radio.FIXTURE_NAME),
                                     (other, [radio.SERVICE], 'private-other-name')])
        output.assert_called_once_with(
            'NXRS_HOST_GATT_DISCOVERY service_reports=2 named_reports=1 named_without_service=0 '
            'os_name_reports=1 os_name_without_service=0 matched_devices=2', flush=True)

    async def test_cancellation_reports_original_counts_without_returning_a_match(self):
        own = types.SimpleNamespace(address='private-fixture', name=None)
        with patch('builtins.print') as output:
            with self.assertRaises(radio.asyncio.CancelledError):
                await self.discover([(own, [radio.SERVICE])], failure=radio.asyncio.CancelledError())
        output.assert_called_once_with(
            'NXRS_HOST_GATT_DISCOVERY service_reports=1 named_reports=0 named_without_service=0 '
            'os_name_reports=0 os_name_without_service=0 matched_devices=1', flush=True)

    async def test_report_counts_are_reset_between_scan_windows(self):
        own = types.SimpleNamespace(address='private-fixture', name=radio.FIXTURE_NAME)
        with patch('builtins.print') as output:
            for events in ([(own, [], radio.FIXTURE_NAME)], []):
                with self.assertRaises(RuntimeError):
                    await self.discover(events)
        self.assertEqual(output.call_count, 2)
        self.assertEqual(output.call_args.args[0],
                         'NXRS_HOST_GATT_DISCOVERY service_reports=0 named_reports=0 '
                         'named_without_service=0 os_name_reports=0 '
                         'os_name_without_service=0 matched_devices=0')

    def test_independent_ble_witness_filters_the_same_canonical_service(self):
        source = (path.parent.parent / 'ble/src/main.rs').read_text()
        self.assertIn('then_some(ble_client_fixture::SERVICE)', source)
        shared = (path.parent.parent / 'ble/src/scenario.rs').read_text()
        data = re.search(r'pub const SERVICE: Uuid = Uuid\(\[([^]]+)\]', shared).group(1)
        actual = bytes(int(value, 16) for value in re.findall(r'0x([0-9a-fA-F]{2})', data))
        self.assertEqual(actual, uuid.UUID(radio.SERVICE).bytes)


if __name__ == '__main__':
    unittest.main()
