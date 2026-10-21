"""Readiness control regressions; no services, networks or certificate trust changes."""
import asyncio
import unittest
from unittest.mock import AsyncMock

from combined_viewer import verified_health


class ConnectorFailure(Exception):
    pass


class CertificateFailure(ConnectorFailure):
    """Mirror aiohttp's certificate-error-is-a-connector-error relationship."""


OK = (200, b'{"status":"ok"}', None)


class VerifiedHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_verified_probe_returns_immediately(self):
        probe = AsyncMock(return_value=OK)
        self.assertEqual(await verified_health(probe), {'status': 200, 'healthy': True})
        self.assertEqual(probe.await_count, 1)

    async def test_delayed_listener_and_proxy_recover_within_one_budget(self):
        probe = AsyncMock(side_effect=[ConnectorFailure(), (502, b'not ready', None),
                                       (503, b'', None), (504, b'', None), OK])
        result = await verified_health(probe, transient_errors=(ConnectorFailure,), interval=.001)
        self.assertTrue(result['healthy'])
        self.assertEqual(probe.await_count, 5)

    async def test_certificate_failure_is_not_retried_as_connector_error(self):
        probe = AsyncMock(side_effect=[CertificateFailure(), OK])
        with self.assertRaises(CertificateFailure):
            await verified_health(probe, transient_errors=(ConnectorFailure,),
                                  permanent_errors=(CertificateFailure,), interval=.001)
        self.assertEqual(probe.await_count, 1)

    async def test_redirect_and_authorization_fail_without_retry(self):
        for status in (301, 302, 307, 308, 400, 401, 403, 404, 429, 500):
            with self.subTest(status=status):
                probe = AsyncMock(side_effect=[(status, b'secret body', None), OK])
                with self.assertRaisesRegex(ValueError, '^unexpected health status$'):
                    await verified_health(probe, interval=.001)
                self.assertEqual(probe.await_count, 1)

    async def test_wrong_or_malformed_health_is_not_readiness(self):
        for body in (b'', b'secret', b'[]', b'{"status":"offline"}', b'{"status":"ok","extra":1}', b'\xff'):
            with self.subTest(body=body):
                probe = AsyncMock(side_effect=[(200, body, None), OK])
                with self.assertRaisesRegex(ValueError, '^invalid health response$'):
                    await verified_health(probe, interval=.001)
                self.assertEqual(probe.await_count, 1)

    async def test_response_limit_and_protocol_errors_remain_fatal(self):
        probe = AsyncMock(side_effect=[ValueError('response exceeds bound'), OK])
        with self.assertRaisesRegex(ValueError, 'response exceeds bound'):
            await verified_health(probe, transient_errors=(ConnectorFailure,), interval=.001)
        self.assertEqual(probe.await_count, 1)

    async def test_deadline_cancels_an_inflight_probe(self):
        retired = asyncio.Event()
        async def probe():
            try:
                await asyncio.Event().wait()
            finally:
                retired.set()
        with self.assertRaises(TimeoutError):
            await verified_health(probe, timeout=.02, interval=.001)
        self.assertTrue(retired.is_set())

    async def test_repeated_transients_do_not_extend_deadline(self):
        probe = AsyncMock(side_effect=ConnectorFailure())
        with self.assertRaises(TimeoutError):
            await verified_health(probe, transient_errors=(ConnectorFailure,),
                                  timeout=.03, interval=.001)
        self.assertGreater(probe.await_count, 1)

    async def test_outer_cancellation_is_not_a_retry(self):
        entered, retired = asyncio.Event(), asyncio.Event()
        async def probe():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                retired.set()
        task = asyncio.create_task(verified_health(probe, transient_errors=(TimeoutError,)))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(retired.is_set())

    async def test_invalid_budgets_rejected_before_network(self):
        probe = AsyncMock(return_value=OK)
        for key in ('timeout', 'interval'):
            for value in (0, -1, True, float('inf'), float('nan')):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    await verified_health(probe, **{key: value})
        probe.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
