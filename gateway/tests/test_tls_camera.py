"""Connection shutdown joins the test-only TLS bridge's socket owners."""
import asyncio
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tls_camera import TlsCameraBridge


class Reader:
    def __init__(self, end, started=None):
        self.end = end
        self.started = started

    async def read(self, _):
        if self.started is not None:
            self.started.set()
        await self.end.wait()
        return b''


class Writer:
    def __init__(self, gate=None):
        self.gate = gate
        self.closing = asyncio.Event()
        self.joined = False
        self.cancelled = False

    def get_extra_info(self, name):
        if name == 'peercert':
            return {'subjectAltName': (('URI', 'fixture-ingest'),)}
        if name == 'ssl_object':
            return SimpleNamespace(getpeercert=lambda **_: b'fixture-peer')
        raise AssertionError('unexpected fixture metadata')

    def close(self):
        self.closing.set()

    async def wait_closed(self):
        try:
            if self.gate is not None:
                await self.gate.wait()
            self.joined = True
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class TlsCameraRetirementTests(unittest.IsolatedAsyncioTestCase):
    def bridge(self):
        bridge = TlsCameraBridge()
        bridge.tasks = set()
        bridge.retiring = set()
        bridge.active = False
        bridge.peak = 0
        bridge.source = SimpleNamespace(port=1234)
        bridge.expected = 'fixture-ingest'
        bridge.client_digest = hashlib.sha256(b'fixture-peer').digest()
        class Server:
            closed = False
            def close(self):
                self.closed = True
            async def wait_closed(self):
                pass
        bridge.server = Server()
        return bridge

    async def test_shutdown_joins_a_connection_already_closing_its_sockets(self):
        bridge = self.bridge()
        ended = asyncio.Event(); ended.set()
        gate = asyncio.Event()
        upstream, client = Writer(gate), Writer()
        with patch('tls_camera.asyncio.open_connection', return_value=(Reader(asyncio.Event()), upstream)):
            connection = asyncio.create_task(bridge.accept(Reader(ended), client))
            await asyncio.wait_for(upstream.closing.wait(), 1)
            shutdown = asyncio.create_task(bridge.close())
            await asyncio.sleep(0)  # Let shutdown inspect/cancel the current owners.
            gate.set()
            await asyncio.wait_for(shutdown, 1)
            await asyncio.wait_for(connection, 1)
        self.assertTrue(bridge.server.closed)
        self.assertTrue(upstream.joined and client.joined)
        self.assertFalse(upstream.cancelled or client.cancelled)
        self.assertFalse(bridge.active or bridge.tasks or bridge.retiring)

    async def test_shutdown_cancels_forwarding_then_joins_both_socket_owners(self):
        bridge = self.bridge()
        started = asyncio.Event()
        upstream, client = Writer(), Writer()
        async def connect(*_, **__):
            return Reader(asyncio.Event()), upstream
        with patch('tls_camera.asyncio.open_connection', side_effect=connect):
            connection = asyncio.create_task(bridge.accept(Reader(asyncio.Event(), started), client))
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.wait_for(bridge.close(), 1)
            await asyncio.gather(connection, return_exceptions=True)
        self.assertTrue(upstream.joined and client.joined)
        self.assertFalse(bridge.active or bridge.tasks or bridge.retiring)
        await bridge.close()


if __name__ == '__main__':
    unittest.main()
