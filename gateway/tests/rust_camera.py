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


def validate_viewer_sharing(before, after):
    """Viewer attachment must preserve the established source, including retry counters."""
    before, after = validate_snapshot(before), validate_snapshot(after)
    if not (before['active'] == after['active'] == 1
            and before['peak'] == after['peak'] == 1
            and before['connections'] == after['connections']
            and before['busy'] == after['busy']):
        raise AssertionError('two viewers must share the established Rust source without new admissions '
                             f'or busy retries: before={before} after={after}')


class RustCamera:
    def __init__(self, process):
        self.process = process
        self.lock = asyncio.Lock()
        self.port = None

    @classmethod
    async def start(cls, binary, directory, detections=False, colors=False, cadence_17fps=False):
        directory = Path(directory)
        if colors:
            for index, color in enumerate(['red', 'blue'], 1):
                image = f'color={color}:size=320x240:rate=10'
                if cadence_17fps:
                    # The near-17 FPS client case keeps its exact center-color
                    # witness but adds deterministic detail. Tiny flat H264
                    # frames otherwise batch in the publisher's demux buffers,
                    # measuring burst coalescing instead of steady rendering.
                    image += f',noise=alls=100:all_seed=7:allf=u,drawbox=x=80:y=60:w=160:h=120:color={color}:t=fill'
                subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                    '-i', image, '-frames:v', '1', '-threads', '1',
                    '-c:v', 'mjpeg', '-q:v', '2', str(directory/f'rust-frame-{index}.jpg')],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        else:
            subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                            '-i', 'testsrc2=size=320x240:rate=10', '-frames:v', '2', '-threads', '1',
                            '-c:v', 'mjpeg', '-q:v', '5', str(directory/'rust-frame-%d.jpg')],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        args = [str(binary), str(directory/'rust-frame-1.jpg'), str(directory/'rust-frame-2.jpg')]
        if detections: args.append('--detection-metadata')
        if cadence_17fps: args.append('--cadence-17fps')
        process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
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
