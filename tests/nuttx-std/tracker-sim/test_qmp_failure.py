"""Protocol controls for temporary diagnostics; these do not qualify a guest."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import qmp_failure


class FailureRegisters(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, reject=None, stall=False, close_fails=False):
        with TemporaryDirectory(prefix='nxrs-qmp-') as folder:
            root = Path(folder)
            image = root / 'nuttx.merged.bin'
            image.write_bytes(b'frozen firmware')
            requests = []
            stopped = False

            reader = asyncio.StreamReader()
            reader.feed_data(b'{"QMP":{"version":{}}}\n')
            case = self

            class Peer:
                # Exercise actual JSON request/response bytes without requiring
                # permission to bind a local socket in the host-test sandbox.
                def write(self, raw):
                    nonlocal stopped
                    request = json.loads(raw)
                    requests.append(request)
                    command = request['execute']
                    if stall:
                        return
                    if command == reject:
                        response = {'error': {'class': 'GenericError', 'desc': 'injected refusal'}}
                    else:
                        result = {}
                        if command == 'stop':
                            stopped = True
                            reader.feed_data(b'{"event":"STOP"}\n')
                        elif command == 'query-status':
                            result = {'running': not stopped, 'status': 'paused'}
                        elif command == 'query-cpus-fast':
                            result = [{'cpu-index': 0}, {'cpu-index': 1}]
                        elif command == 'human-monitor-command':
                            case.assertTrue(stopped)
                            case.assertEqual(request['arguments']['command-line'], 'info registers')
                            result = f"CPU{request['arguments']['cpu-index']} DEPC=40374300 EPC1=42012340"
                        elif command == 'pmemsave':
                            case.assertTrue(stopped)
                            case.assertEqual(request['arguments']['val'], 0x3FC80000)
                            case.assertEqual(request['arguments']['size'], 0x170000)
                            Path(request['arguments']['filename']).write_bytes(b'\xa5' * 0x170000)
                        response = {'return': result}
                    response['id'] = request['id']
                    reader.feed_data(json.dumps(response).encode() + b'\n')

                async def drain(self):
                    pass

                def close(self):
                    reader.feed_eof()
                    if close_fails:
                        raise OSError('injected close failure')

                async def wait_closed(self):
                    pass

            error = TimeoutError('complete panel frame')
            try:
                try:
                    raise error
                except Exception as original:
                    with (patch.object(qmp_failure, 'TIMEOUT_SECONDS', 0.1 if stall else 2),
                          patch.object(asyncio, 'open_unix_connection', return_value=(reader, Peer()))):
                        await qmp_failure.retain(root / 'qmp', image, root / 'registers.json', original)
                    raise
            except TimeoutError as reraised:
                self.assertIs(reraised, error)
            return requests, json.loads((root / 'registers.json').read_text())

    async def test_stops_before_capturing_each_cpu_and_keeps_raw_registers(self):
        requests, report = await self.exercise()
        self.assertEqual([r['execute'] for r in requests], [
            'qmp_capabilities', 'stop', 'query-status', 'query-cpus-fast',
            'human-monitor-command', 'human-monitor-command', 'pmemsave'])
        self.assertEqual([r['arguments']['cpu-index'] for r in requests[4:6]], [0, 1])
        self.assertEqual(report['registers']['1'], 'CPU1 DEPC=40374300 EPC1=42012340')
        self.assertEqual(report['events'], [{'event': 'STOP'}])
        self.assertEqual(report['original_error']['message'], 'complete panel frame')
        self.assertEqual(report['dram']['bytes'], 0x170000)
        self.assertEqual(len(report['dram']['sha256']), 64)
        self.assertNotIn('diagnostic_error', report)

    async def test_qmp_refusal_retains_partial_evidence_and_original_failure(self):
        requests, report = await self.exercise(reject='query-cpus-fast')
        self.assertEqual(requests[-1]['execute'], 'query-cpus-fast')
        self.assertIn('injected refusal', report['diagnostic_error']['message'])
        self.assertEqual(report['registers'], {})

    async def test_stalled_monitor_is_bounded_and_preserves_original_failure(self):
        requests, report = await asyncio.wait_for(self.exercise(stall=True), 2)
        self.assertEqual(len(requests), 1)
        self.assertEqual(report['diagnostic_error']['type'], 'TimeoutError')

    async def test_transport_close_failure_keeps_registers_and_original_failure(self):
        _, report = await self.exercise(close_fails=True)
        self.assertEqual(report['close_error'], 'injected close failure')
        self.assertEqual(set(report['registers']), {'0', '1'})

    async def test_memory_dump_refusal_keeps_the_register_snapshot(self):
        _, report = await self.exercise(reject='pmemsave')
        self.assertEqual(set(report['registers']), {'0', '1'})
        self.assertIn('injected refusal', report['diagnostic_error']['message'])
        self.assertNotIn('sha256', report['dram'])

    async def test_unwritable_evidence_does_not_replace_original_failure(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            error = TimeoutError('original')
            await qmp_failure.retain(root / 'missing-qmp', root / 'missing-image', root, error)
            self.assertIn('could not be saved', error.__notes__[-1])


if __name__ == '__main__':
    unittest.main()
