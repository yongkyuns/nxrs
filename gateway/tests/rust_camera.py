"""Async control of a native owner executing the real Rust stream-service source."""
import asyncio
import json
from pathlib import Path
import subprocess


def validate_snapshot(result):
    keys = ['active', 'peak', 'connections', 'disconnected', 'frames', 'busy']
    if not isinstance(result, dict) or not all(type(result.get(key)) is int and result[key] >= 0 for key in keys):
        raise AssertionError('bounded Rust fixture counters')
    if not (result['active'] <= result['peak'] <= 1
            and result['connections'] - result['disconnected'] == result['active']):
        raise AssertionError('actual server admitted-client accounting')
    # Busy rejects are NOT admissions. A FIN retains the response direction until
    # the actual server's idle/write deadline, so a bounded reconnect may be rejected.
    return result


class RustCamera:
    def __init__(self, process):
        self.process = process
        self.lock = asyncio.Lock()
        self.port = None

    @classmethod
    async def start(cls, binary, directory):
        directory = Path(directory)
        subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                        '-i', 'testsrc2=size=320x240:rate=10', '-frames:v', '2', '-threads', '1',
                        '-c:v', 'mjpeg', '-q:v', '5', str(directory/'rust-frame-%d.jpg')],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        process = await asyncio.create_subprocess_exec(str(binary), str(directory/'rust-frame-1.jpg'),
            str(directory/'rust-frame-2.jpg'), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1024)
        instance = cls(process)
        try:
            ready = await instance._read()
            if ready.get('implementation') != 'rust-service-stream' or not 1 <= ready.get('port', 0) <= 65535:
                raise AssertionError('real Rust camera fixture readiness')
            instance.port = ready['port']
            return instance
        except BaseException:
            await instance.close()
            raise

    async def _read(self):
        raw = await asyncio.wait_for(self.process.stdout.readline(), 5)
        if not raw or len(raw) > 1024:
            raise AssertionError('bounded Rust fixture response')
        return json.loads(raw)

    async def command(self, value):
        if value not in ['status', 'pause', 'resume', 'quit']:
            raise ValueError('invalid fixture command')
        async with self.lock:
            self.process.stdin.write((value+'\n').encode())
            await asyncio.wait_for(self.process.stdin.drain(), 5)
            return validate_snapshot(await self._read())

    async def close(self):
        if self.process.returncode is not None:
            return
        try:
            await self.command('quit')
            await asyncio.wait_for(self.process.wait(), 5)
        except (Exception, asyncio.CancelledError):
            if self.process.returncode is None:
                self.process.kill()
                await asyncio.wait_for(self.process.wait(), 5)
            raise
