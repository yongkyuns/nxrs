"""Control the actual LiveCamera/Mbed TLS fixture with synthetic enrollment/time."""
import asyncio
import json
from pathlib import Path
import ssl
import subprocess

from rust_camera import validate_snapshot
from tls_reference import Credentials, private


class TlsCamera:
    def __init__(self, process, directory, credentials):
        self.process, self.directory, self.credentials = process, directory, credentials
        self.lock = asyncio.Lock()
        self.port = None

    @classmethod
    async def start(cls, binary, directory, **identity):
        directory = Path(directory)
        directory.mkdir(mode=0o700)
        subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
            '-i', 'testsrc2=size=320x240:rate=10', '-frames:v', '2', '-threads', '1',
            '-c:v', 'mjpeg', '-q:v', '5', str(directory/'rust-frame-%d.jpg')],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        for path in directory.glob('rust-frame-*.jpg'):
            if not 4 <= path.stat().st_size <= 16384:
                raise AssertionError('TLS fixture JPEG capacity')
        with (directory/'endpoint.log').open('wb') as error:
            process = await asyncio.create_subprocess_exec(str(binary), str(directory), '60000', 'media', '262144',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=error, limit=1024)
        instance = cls(process, directory, None)
        try:
            if await instance._line() != 'CSR_READY':
                raise AssertionError('worker-owned device CSR')
            instance.credentials = Credentials(directory, server_csr=directory/'request.csr', **identity)
            for pem, der in [('server.pem', 'server.der'), ('ca.pem', 'issuer.der'), ('client.pem', 'peer.der')]:
                private(directory/der, ssl.PEM_cert_to_DER_cert((directory/pem).read_text()))
            instance.credentials.call('crl', '-in', directory/'crl.pem', '-outform', 'DER', '-out', directory/'revocations.der')
            process.stdin.write(b'load\n')
            await process.stdin.drain()
            line = await instance._line()
            if not line.startswith('LISTEN 127.0.0.1:'):
                raise AssertionError('TLS camera loopback readiness')
            instance.port = int(line.rsplit(':', 1)[1])
            await instance.command('status')
            return instance
        except BaseException:
            if process.returncode is None:
                process.kill()
            await asyncio.wait_for(process.wait(), 5)
            process.stdin.close()
            raise

    async def _line(self):
        raw = await asyncio.wait_for(self.process.stdout.readline(), 5)
        if not raw or len(raw) > 1024:
            raise AssertionError('bounded TLS camera response')
        return raw.decode('ascii').strip()

    async def command(self, value):
        if value not in ['status', 'pause', 'resume', 'quit']:
            raise ValueError('invalid fixture command')
        async with self.lock:
            if value != 'status':
                (self.directory/'control').write_text('stop' if value == 'quit' else value)
                expected = {'pause': 'PAUSED', 'resume': 'RESUMED', 'quit': 'CAMERA_STOPPED'}[value]
                if await self._line() != expected:
                    raise AssertionError('TLS camera control acknowledgement')
            deadline = asyncio.get_running_loop().time() + 5
            while not (self.directory/'state.json').exists():
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError('TLS camera state deadline')
                await asyncio.sleep(.01)
            with (self.directory/'state.json').open('rb') as stream:
                data = stream.read(1025)
            if len(data) > 1024:
                raise AssertionError('bounded TLS camera state')
            state = json.loads(data)
            return validate_snapshot(dict(active=state['active'], peak=state['peak'],
                connections=state['accepted'], disconnected=state['disconnected'],
                frames=state['frames'], busy=state['rejected_busy']))

    async def close(self):
        try:
            if self.process.returncode is None:
                result = await self.command('quit')
                if result['active'] != 0:
                    raise AssertionError('TLS camera retirement accounting')
                await asyncio.wait_for(self.process.wait(), 5)
            if self.process.returncode != 0:
                raise AssertionError('TLS camera owner failed')
        finally:
            if self.process.returncode is None:
                self.process.kill()
                await asyncio.wait_for(self.process.wait(), 5)
            self.process.stdin.close()
