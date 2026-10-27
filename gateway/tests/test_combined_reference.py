"""Unprivileged regressions. These never create a service, account or network."""
import contextlib
import asyncio
import io
import json
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import combined_reference as full
from combined_viewer import decode_command, read_bounded


class CombinedFixtureTests(unittest.TestCase):
    def test_refuses_personal_machine_before_mutating(self):
        with patch.dict(full.os.environ, {}, clear=True), patch.object(full, 'command') as command:
            with self.assertRaises(AssertionError):
                full.prerequisites()
            command.assert_not_called()

    def test_namespace_adapter_preserves_hardening_and_lifetime(self):
        source = ('[Unit]\nBindsTo=wg-quick@wg-nxrs.service nxrs-control.service nxrs-media.service\n'
                  'PartOf=nxrs-monitor.target\n[Service]\nNoNewPrivileges=yes\n'
                  'ProtectSystem=strict\nReadOnlyPaths=/run\nKillMode=control-group\n'
                  'MemoryMax=256M\nTasksMax=128\nCapabilityBoundingSet=\nRestart=no\n'
                  'ExecStart=/usr/local/libexec/nxrs-camera-ingest /run/nxrs-ingest/config.json --require-media-isolation\n'
                  'ExecStartPre=/usr/local/libexec/nxrs-media-sandbox --check\n'
                  'LoadCredential=config:/etc/nxrs-monitor/ingest.json\n')
        name, output = full.adapt_unit(source, 'nxrs-ingest.service', 'nxrs-test', Path('/run/nxrs-test'), 'nxrs-test-s')
        self.assertEqual(name, 'nxrs-test-ingest.service')
        for value in ['NoNewPrivileges=yes', 'ProtectSystem=strict', 'ReadOnlyPaths=/run',
                      'KillMode=control-group', 'MemoryMax=256M', 'TasksMax=128',
                      'CapabilityBoundingSet=', 'Restart=no']:
            self.assertEqual(output.count(value), 1)
        self.assertIn('BindsTo=nxrs-test-network.service nxrs-test-control.service nxrs-test-media.service', output)
        self.assertIn('NetworkNamespacePath=/run/netns/nxrs-test-s', output)
        self.assertIn('/run/nxrs-test/bin/ingest /run/nxrs-test-ingest/config.json', output)
        self.assertIn('/run/nxrs-test/conf/ingest.json', output)
        self.assertNotIn('/etc/nxrs-monitor', output)
        self.assertIn('ExecStartPre=/run/nxrs-test/bin/media-sandbox --check',output)
        self.assertIn('--require-media-isolation',output)

    def test_proxy_and_failure_handler_keep_admission_and_use_owned_paths(self):
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'deploy'))
        import strict_vpn
        for original in ['nxrs-proxy.service','nxrs-monitor-stop.service']:
            name,text=full.adapt_unit(strict_vpn.services()[original],original,'nxrs-test',Path('/run/nxrs-test'),'nxrs-test-s')
            if original=='nxrs-proxy.service':
                self.assertEqual(name,'nxrs-test-proxy.service')
                self.assertIn('/run/nxrs-test/bin/proxy-launcher --selection /run/nxrs-test/proxy-artifact/active.json',text)
                self.assertIn('OnFailure=nxrs-test-monitor-stop.service',text)
                self.assertIn('--runtime /run/nxrs-test-proxy --config %d/config --check',text)
                self.assertNotIn('/usr/sbin/nginx',text)
            else:
                self.assertEqual(name,'nxrs-test-monitor-stop.service')
                self.assertIn('stop nxrs-test-control.service nxrs-test-media.service nxrs-test-proxy.service nxrs-test-monitor.target',text)
            self.assertIn('CapabilityBoundingSet=',text)
            self.assertIn('NetworkNamespacePath=/run/netns/nxrs-test-s',text)

    def test_network_admission_uses_owned_executable_and_both_owned_state_paths(self):
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'deploy'))
        import strict_vpn
        name,text=full.adapt_unit('[Service]\nExecStart=/usr/bin/wg-quick up /etc/wireguard/wg-nxrs.conf\n'+strict_vpn.vpn.network_admission(),
            'wg-quick@wg-nxrs.service','nxrs-test',Path('/run/nxrs-test'),'nxrs-test-s')
        self.assertEqual(name,'nxrs-test-network.service')
        self.assertIn('ExecStartPre=/run/nxrs-test/bin/vpn-admission ',text)
        self.assertIn('--config /run/nxrs-test/conf/wg-nxrs.conf --history /run/nxrs-test/conf/vpn-peers.json',text)
        self.assertIn('--firewall /run/nxrs-test/conf/firewall.nft',text)
        self.assertIn('NetworkNamespacePath=/run/netns/nxrs-test-s',text)
        self.assertNotIn('/etc/wireguard',text)

    def test_target_has_no_invalid_service_section(self):
        _, target = full.adapt_unit('[Unit]\nWants=nxrs-ingest.service\n', 'nxrs-monitor.target',
                                   'nxrs-test', Path('/run/nxrs-test'), 'nxrs-test-s')
        self.assertNotIn('[Service]', target)
        self.assertIn('Wants=nxrs-test-ingest.service', target)

    def test_cleanup_continues_after_failure(self):
        completed = []
        def failure():
            raise OSError('secret-bearing detail must not escape')
        result = full.cleanup_all([('first', lambda: completed.append(1)), ('failed', failure),
                                   ('last', lambda: completed.append(2))])
        self.assertEqual(completed, [1, 2])
        self.assertEqual(result, ['failed'])

    def test_bounded_fixed_receiver_commands(self):
        for value in [{'op': 'connect', 'viewer': 'a'}, {'op': 'count', 'id': 0}, {'op': 'quit'}]:
            self.assertEqual(decode_command(json.dumps(value).encode()), value)
        for value in [{'op': 'shell'}, {'op': 'connect', 'viewer': 'administrator'},
                      {'op': 'count', 'id': True}, {'op': 'count', 'id': -1}, {'op': 'count', 'id': 8},
                      {'op': 'connect', 'viewer': 'a', 'url': 'https://untrusted.test'}, []]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_command(json.dumps(value).encode())
        with self.assertRaises(ValueError):
            decode_command(b' ' * 1025)

    def test_import_has_no_runtime_dependency_or_privileged_side_effects(self):
        # Runtime aiortc/generator imports are inside the entry points, allowing these
        # policy tests to run in the minimal contract job without media dependencies.
        self.assertFalse(hasattr(full, 'strict_vpn'))
        self.assertFalse(hasattr(full, 'aiohttp'))


class PublicationDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def client(self,status,value):
        response=SimpleNamespace(status=status,json=AsyncMock(return_value=value))
        class Context:
            async def __aenter__(self):return response
            async def __aexit__(self,*args):return False
        return SimpleNamespace(get=Mock(return_value=Context())),response

    async def test_publication_readiness_and_bounded_observations_preserve_each_outcome(self):
        for status,value,ready in ((404,None,False),(503,None,False),(200,{'ready':False},False),
                                   (200,{'ready':True,'private':'hidden'},True),
                                   (200,{'ready':'private detail'},False)):
            with self.subTest(status=status,value=value):
                http,response=self.client(status,value);observed=dict(attempts=0,status=None,ready=None)
                self.assertIs(await full.publication_ready(http,'private-token',observed),ready)
                self.assertEqual(observed['attempts'],1);self.assertEqual(observed['status'],status)
                self.assertEqual(response.json.await_count,int(status==200))
                diagnostic=full.publication_diagnostics(observed)
                self.assertEqual(diagnostic['ready'],value['ready'] if status==200 and type(value['ready']) is bool else None)
                self.assertNotIn('private',json.dumps(diagnostic));self.assertNotIn('hidden',json.dumps(diagnostic))

    async def test_new_probe_clears_stale_success_before_network_or_malformed_json_failure(self):
        for network_failure in (True,False):
            http,response=self.client(200,None)
            if network_failure:http.get.side_effect=RuntimeError('private network detail')
            else:response.json.side_effect=ValueError('private malformed response')
            observed=dict(attempts=1,status=200,ready=True)
            with self.subTest(network_failure=network_failure),self.assertRaises((RuntimeError,ValueError)):
                await full.publication_ready(http,'private-token',observed)
            self.assertEqual(observed['attempts'],2);self.assertIsNone(observed['ready'])
            self.assertEqual(observed['status'],None if network_failure else 200)

    async def test_publication_deadline_is_still_a_failure(self):
        http,_=self.client(404,None);observed=dict(attempts=0,status=None,ready=None)
        with self.assertRaisesRegex(AssertionError,'source publication not ready'):
            await full.until(lambda:full.publication_ready(http,'private-token',observed),'source publication not ready',seconds=.01)
        self.assertGreater(observed['attempts'],0);self.assertIsNone(observed['ready'])

    def test_diagnostic_projectors_reject_unknown_shapes_types_and_unbounded_counters(self):
        good=dict(active=1,peak=1,connections=2,disconnected=1,frames=30,busy=0,private='hidden')
        self.assertEqual(full.source_diagnostics(good),{k:v for k,v in good.items() if k!='private'}|{'available':True})
        for key,value in (('active',True),('peak',2),('connections',0),('frames',2**53),('busy',-1)):
            with self.subTest(key=key):
                self.assertFalse(full.source_diagnostics({**good,key:value})['available'])
        for value in (None,{},dict(attempts=True,status=200,ready=True),dict(attempts=1,status=600,ready=None),
                      dict(attempts=1000001,status=200,ready=None),dict(attempts=1,status=200,ready='hidden')):
            self.assertEqual(full.publication_diagnostics(value),dict(available=False))

    async def test_failure_output_is_bounded_and_never_exports_exception_or_extra_source_fields(self):
        error=None
        try:exec(compile("raise AssertionError('private exception detail')",str(full.HERE/'combined_reference.py'),'exec'))
        except AssertionError as caught:error=caught
        for available in (True,False):
            camera=SimpleNamespace(command=AsyncMock(return_value=dict(active=1,peak=1,connections=1,disconnected=0,frames=12,busy=0,token='hidden')))
            if not available:camera.command.side_effect=RuntimeError('private camera detail')
            out=io.StringIO()
            with contextlib.redirect_stdout(out):
                await full.failure_diagnostics(error,camera,dict(attempts=3,status=404,ready=None,token='hidden'))
            camera.command.assert_awaited_once_with('status')
            text=out.getvalue();self.assertLess(len(text),2048)
            for secret in ('private','hidden','token',str(full.HERE)):self.assertNotIn(secret,text)
            rows=text.splitlines();self.assertEqual(len(rows),3)
            self.assertIn('"source": "gateway/tests/combined_reference.py"',rows[0])
            self.assertEqual(json.loads(rows[2].removeprefix('COMBINED SOURCE:'))['available'],available)

    async def test_cancellation_during_source_diagnostics_retires_the_pending_exchange(self):
        entered,retired=asyncio.Event(),asyncio.Event()
        async def command(operation):
            self.assertEqual(operation,'status');entered.set()
            try:await asyncio.Event().wait()
            finally:retired.set()
        with contextlib.redirect_stdout(io.StringIO()):
            task=asyncio.create_task(full.failure_diagnostics(AssertionError('private'),SimpleNamespace(command=command),{}))
            await entered.wait();task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(retired.is_set())


class FragmentedResponseTests(unittest.IsolatedAsyncioTestCase):
    async def response(self, status, payload):
        class Response:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def read(self): return payload
        response = Response()
        response.status = status
        class Client:
            def request(inner, method, url, **kwargs):
                self.assertEqual(url, 'http://127.0.0.1:18081/internal/test')
                self.assertFalse(kwargs['allow_redirects'])
                return response
        return await full.internal_request(Client(), 'POST', '/internal/test', {})

    async def test_status_only_revocation_acknowledgment_does_not_require_json(self):
        self.assertEqual(await self.response(202, b''), (202, None))

    async def test_status_endpoint_keeps_its_json_body(self):
        self.assertEqual(await self.response(200, b'{"media_control_ready":true}'),
                         (200, {'media_control_ready': True}))

    async def test_nonempty_malformed_internal_response_remains_fatal(self):
        with self.assertRaises(json.JSONDecodeError):
            await self.response(200, b'not json')

    async def test_reads_every_fragment_until_eof(self):
        class Stream:
            def __init__(self): self.parts = [b'{', b'"ok":', b'true}', b'']
            async def read(self, n): return self.parts.pop(0)
        self.assertEqual(await read_bounded(Stream()), b'{"ok":true}')

    async def test_reads_only_one_byte_beyond_limit_before_rejecting(self):
        class Stream:
            def __init__(self): self.requests = []
            async def read(self, n):
                self.requests.append(n)
                return b'x' * n
        stream = Stream()
        with self.assertRaises(ValueError): await read_bounded(stream, limit=9000)
        self.assertEqual(stream.requests, [8192, 809])


if __name__ == '__main__':
    unittest.main()
