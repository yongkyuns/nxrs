#!/usr/bin/env python3
"""Pipe-controlled WebRTC receiver in the isolated phone namespace, never an iPhone.

Only fixed operations, integer counts and HTTP statuses leave the process. Account
and media credentials stay in a private input file or memory, never argv/output.
"""
import argparse
import asyncio
import json
import math
from pathlib import Path
import ssl
import sys
from urllib.parse import urljoin, urlsplit

# A surviving client retries a lost kernel WireGuard session after roughly
# KEEPALIVE_TIMEOUT (10s) + REKEY_TIMEOUT (5s) + jitter. The intentional server
# interface removal must allow that handshake and a subsequent HTTPS attempt.
# https://git.zx2c4.com/wireguard-linux/tree/drivers/net/wireguard/timers.c
HEALTH_TIMEOUT = 45.0


def decode_command(raw):
    if not raw or len(raw) > 1024:
        raise ValueError('bounded command required')
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) - {'op', 'viewer', 'id'}:
        raise ValueError('invalid command shape')
    if value.get('op') not in ['health', 'catalog', 'connect', 'count', 'delete', 'quit']:
        raise ValueError('invalid operation')
    if value['op'] in ['catalog', 'connect'] and value.get('viewer') not in ['a', 'b']:
        raise ValueError('invalid viewer')
    if value['op'] in ['count', 'delete'] and (type(value.get('id')) is not int or not 0 <= value['id'] < 8):
        raise ValueError('invalid reader ID')
    return value


async def read_bounded(stream, limit=65536):
    """read(n) can return a fragment: assemble to EOF without an unbounded read."""
    chunks = bytearray()
    while True:
        part = await stream.read(min(8192, limit + 1 - len(chunks)))
        if not part:
            return bytes(chunks)
        chunks.extend(part)
        if len(chunks) > limit:
            raise ValueError('response exceeds bound')


async def verified_health(probe, *, transient_errors=(), permanent_errors=(),
                          timeout=HEALTH_TIMEOUT, interval=.1):
    """Wait for verified HTTPS readiness, not merely systemd Type=simple activation.

    One deadline covers every attempt and delay. Certificate/TLS failures precede
    connector failures because aiohttp certificate errors also inherit the latter.
    Retries are only for startup connectivity and explicit proxy-unavailable codes;
    redirects, authorization errors, malformed/oversized replies remain failures.
    """
    for value in (timeout, interval):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError('finite positive readiness budget required')
    async with asyncio.timeout(timeout):
        while True:
            try:
                status, body, _ = await probe()
            except permanent_errors:
                raise
            except transient_errors:
                pass
            else:
                if status == 200:
                    try:
                        healthy = json.loads(body) == {'status': 'ok'}
                    except (ValueError, UnicodeError):
                        healthy = False
                    if not healthy:
                        raise ValueError('invalid health response')
                    return {'status': 200, 'healthy': True}
                if status not in (502, 503, 504):
                    raise ValueError('unexpected health status')
            await asyncio.sleep(interval)


async def exercise(path):
    import aiohttp
    from aiortc import RTCSessionDescription
    from revocation_reference import Reader, require

    config = json.loads(Path(path).read_text())
    origin = config['origin']
    require(origin == 'https://10.77.0.1:8443', 'fixed isolated tunnel origin')
    tls = ssl.create_default_context(cafile=config['ca'])
    input_stream = asyncio.StreamReader(limit=1024)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(input_stream), sys.stdin.buffer)
    readers = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
        async def request(method, url, token=None, **kwargs):
            headers = kwargs.pop('headers', {})
            if token is not None:
                headers['Authorization'] = 'Bearer ' + token
            async with http.request(method, url, headers=headers, ssl=tls,
                                    allow_redirects=False, **kwargs) as response:
                raw = await read_bounded(response.content)
                return response.status, raw, response.headers.get('Location')

        async def connect(viewer):
            require(len(readers) < 8, 'bounded receiver count')
            reader = Reader()
            # Retain every created peer before awaiting, including partial negotiation failures.
            slot = {'peer': reader, 'token': None, 'url': None}
            readers.append(slot)
            await reader.peer.setLocalDescription(await reader.peer.createOffer())
            status, body, _ = await request('POST', origin + '/v1/cameras/cam_reference/playback-grants',
                config['viewers'][viewer], json={})
            require(status == 201, 'authorized playback grant')
            grant = json.loads(body)
            require(grant['camera_id'] == 'cam_reference' and grant['endpoint'] == origin + '/reference/whep',
                    'fixed scoped endpoint')
            slot['token'] = grant['access_token']
            status, answer, location = await request('POST', grant['endpoint'], slot['token'],
                data=reader.peer.localDescription.sdp, headers={'Content-Type': 'application/sdp'})
            require(status == 201 and location, 'WHEP negotiation')
            session = urljoin(grant['endpoint'], location)
            parsed = urlsplit(session)
            require(parsed.scheme == 'https' and parsed.netloc == urlsplit(origin).netloc
                    and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
                    'same-origin session location')
            slot['url'] = session
            await reader.peer.setRemoteDescription(RTCSessionDescription(sdp=answer.decode(), type='answer'))
            while reader.count < 5:
                await asyncio.sleep(.05)
            return {'id': len(readers) - 1, 'count': reader.count}

        async def operate(command):
            op = command['op']
            if op == 'health':
                return await verified_health(lambda: request('GET', origin + '/healthz'),
                    transient_errors=(aiohttp.ClientConnectorError, aiohttp.ServerDisconnectedError,
                                      asyncio.TimeoutError),
                    permanent_errors=(aiohttp.ClientSSLError, ssl.SSLError))
            if op == 'catalog':
                status, _, _ = await request('GET', origin + '/v1/cameras', config['viewers'][command['viewer']])
                return {'status': status}
            if op == 'connect':
                return await connect(command['viewer'])
            if op in ['count', 'delete']:
                slot = readers[command['id']]
                if op == 'count':
                    return {'count': slot['peer'].count}
                status, _, _ = await request('DELETE', slot['url'], slot['token'])
                require(status in [200, 204], 'active WHEP deletion')
                status, _, _ = await request('DELETE', slot['url'], slot['token'])
                require(status == 404, 'deleted session absent')
                slot['url'] = None
                await slot['peer'].close()
                return {'deleted': True}
            return {'quit': True}

        try:
            while True:
                raw = await asyncio.wait_for(input_stream.readline(), 120)
                if not raw:
                    break
                command = decode_command(raw)
                try:
                    budget = HEALTH_TIMEOUT + 5 if command['op'] == 'health' else 20
                    result = await asyncio.wait_for(operate(command), budget)
                except Exception:
                    # No remote bodies, exception messages, URLs or credentials in the protocol.
                    result = {'error': 'operation_failed'}
                print(json.dumps(result), flush=True)
                if command['op'] == 'quit':
                    break
        finally:
            transport.close()
            failures = []
            for slot in readers:
                try:
                    # The test explicitly verifies a healthy DELETE. A dead server may make
                    # remote cleanup impossible; always release the local decoder regardless.
                    if slot['url'] is not None:
                        try:
                            await asyncio.wait_for(request('DELETE', slot['url'], slot['token']), 1)
                        except Exception:
                            pass
                finally:
                    try:
                        await asyncio.wait_for(slot['peer'].close(), 5)
                    except Exception:
                        failures.append('peer')
            require(not failures, 'receiver cleanup')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('configuration', type=Path)
    args = parser.parse_args()
    try:
        asyncio.run(exercise(args.configuration))
    except Exception:
        print('{"error":"receiver_failed"}', flush=True)
        raise SystemExit(1) from None
