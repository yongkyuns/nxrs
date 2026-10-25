"""Unprivileged regressions. These never create a service, account or network."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

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
                self.assertIn('/run/nxrs-test/bin/proxy-launcher --policy /run/nxrs-test/proxy-artifact/approval.json',text)
                self.assertIn('OnFailure=nxrs-test-monitor-stop.service',text)
                self.assertIn('--runtime /run/nxrs-test-proxy --config %d/config --check',text)
                self.assertNotIn('/usr/sbin/nginx',text)
            else:
                self.assertEqual(name,'nxrs-test-monitor-stop.service')
                self.assertIn('stop nxrs-test-control.service nxrs-test-media.service nxrs-test-proxy.service nxrs-test-monitor.target',text)
            self.assertIn('CapabilityBoundingSet=',text)
            self.assertIn('NetworkNamespacePath=/run/netns/nxrs-test-s',text)

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
