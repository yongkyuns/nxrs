#!/usr/bin/env python3
"""Qualify annotated H.264 and Opus through actual ingest with an isolated mTLS camera fixture."""
import argparse
from array import array
import asyncio
from contextlib import ExitStack
from copy import deepcopy
import json
import math
import re
from pathlib import Path
import ssl
import struct
import subprocess
import tempfile
import threading
import time

import av
from matter_rtsp_reference import start_media
from matter_gateway_reference import start
from tls_reference import Credentials, private

BOUNDARY = b'--nxrs-frame\r\n'
SEI = b'nxrs-video-v1\x00\x00\x00'
SAMPLES = 320
CLOCK_START = 8_000_000
ROOT = Path(__file__).resolve().parents[2]

def part(kind, body, headers):
    fields = f'Content-Type: {kind}\r\nContent-Length: {len(body)}\r\n'
    if headers:
        fields += headers + '\r\n'
    fields += '\r\n'
    return BOUNDARY + fields.encode() + body + b'\r\n'

def jpeg(ffmpeg, color):
    output = subprocess.check_output([
        ffmpeg, '-nostdin', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=320x240:r=30',
        '-vf', f'drawbox=x=0:y=0:w=80:h=80:color={color}:t=fill',
        '-frames:v', '1', '-threads', '1', '-c:v', 'mjpeg', '-f', 'image2pipe', 'pipe:1',
    ], timeout=10)
    assert 4 <= len(output) <= 153600
    return output

async def camera(reader, writer, cert, frames, received, allow_audio):
    try:
        peer = writer.get_extra_info('ssl_object').getpeercert(binary_form=True)
        assert peer == cert
        request = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 3)
        assert request.split(b'\r\n', 1)[0] == b'GET /stream?av=2 HTTP/1.1'
        received.append(True)
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=nxrs-frame\r\nX-Nxrs-AV: 2\r\nConnection: close\r\n\r\n')
        await writer.drain()
        command = await asyncio.wait_for(reader.readexactly(9), 3)
        assert command == struct.pack('>QB', 1, 0)
        first_audio = None
        tone = struct.pack('<320h', *(round(18_000 * math.sin(i * math.pi / 8)) for i in range(SAMPLES)))
        template = json.loads((ROOT / 'clients/ios/Tests/NxrsMonitorCoreTests/Fixtures/detection.json').read_text())['source']
        began = time.monotonic()
        for index in range(1500):
            stamp = CLOCK_START + index * 20
            seq = index // 3 + 1
            if first_audio is None and allow_audio.is_set():
                first_audio = index
                writer.write(part('application/vnd.nxrs.microphone-state.v2', command, ''))
            if first_audio is not None:
                writer.write(part('application/vnd.nxrs.pcm16.v1', tone if seq % 2 else bytes(640),
                                  f'X-First-Sample: {(index-first_audio)*SAMPLES}\r\nX-Capture-Ms: {stamp}'))
            if index % 3 == 0:
                source = deepcopy(template)
                source.update(publication_id=seq, emitted_ms=stamp,
                              presence='person' if seq % 2 else 'clear', event_count=0, events=[])
                source['latest'].update(frame_sequence=seq, captured_ms=stamp)
                if seq % 2 == 0:
                    source['latest'].update(person_score_permille=0, objects=[])
                metadata = json.dumps(source, separators=(',', ':')).encode()
                writer.write(part('application/vnd.nxrs.detection+json', metadata, ''))
                writer.write(part('image/jpeg', frames[(seq-1) % 2], f'X-Sequence: {seq}\r\nX-Capture-Ms: {stamp}'))
            await writer.drain()
            await asyncio.sleep(max(0, began + (index+1)*.02 - time.monotonic()))
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

def receive(port, token, allow_audio):
    # Suppress SDK logs and exceptions that could include the temporary RTSP token.
    av.logging.set_level(av.logging.PANIC)
    url = f'rtsp://reader:{token}@127.0.0.1:{port}/cam'
    frames = []
    audio = []
    mono = av.AudioResampler(format="s16", layout="mono", rate=16_000)
    seen = set()
    video_steps = []
    silence_frames = 0
    try:
        with av.open(url, options={'rtsp_transport': 'tcp'}, timeout=(4, 5)) as streams:
            assert [(s.type, s.codec_context.name) for s in streams.streams] == [('video', 'h264'), ('audio', 'opus')]
            picture = av.CodecContext.create('h264', 'r')
            picture.thread_count = 1
            picture.extradata = streams.streams.video[0].codec_context.extradata
            def decode():
                joined = False
                for packet in streams.demux(video=0, audio=0):
                    if packet.stream.type == 'video':
                        if not joined:
                            if not packet.is_keyframe:
                                continue
                            joined = True
                        if packet.size:
                            data = bytes(packet)
                            kinds = [n[0] & 31 for n in re.split(b'\x00\x00\x00?\x01', data) if n]
                            count = data.count(b'nxrs-video-v1')
                            assert count == 1, (len(frames), packet.pts, packet.is_keyframe, count, kinds)
                        yield from picture.decode(packet)
                    else:
                        yield from packet.decode()
            end = time.monotonic() + 12
            for frame in decode():
                assert time.monotonic() < end
                if isinstance(frame, av.VideoFrame):
                    preview = frame.reformat(format='rgb24')
                    rgb = preview.planes[0]
                    at = 40 * rgb.line_size + 40 * 3
                    red, _, blue = bytes(rgb)[at:at + 3]
                    is_red = red - blue > 100
                    is_blue = blue - red > 100
                    assert is_red != is_blue, (len(frames), red, blue)
                    records = [json.loads(data[len(SEI):]) for side in frame.side_data
                               if (data := bytes(side)).startswith(SEI)]
                    assert len(records) == 1, (len(frames), frame.pts, is_red, [record['frame_sequence'] for record in records])
                    metadata, = records
                    seq = metadata['frame_sequence']
                    assert seq % 2 == int(is_red), (len(frames), seq, red, blue, frame.key_frame, frame.pts)
                    assert metadata['frame_capture_ms'] == CLOCK_START + (seq-1)*60
                    assert metadata['source']['latest']['frame_sequence'] == seq
                    assert metadata['source']['presence'] == ('person' if is_red else 'clear')
                    seen.add(metadata['epoch'])
                    if frames:
                        prev_seq, prev_timestamp = frames[-1]
                        assert seq == prev_seq + 1
                        step = float(frame.pts * frame.time_base - prev_timestamp)
                        assert step > 0
                        video_steps.append(step)
                    frames.append((seq, frame.pts * frame.time_base))
                else:
                    assert frame.sample_rate == 48000
                    for chunk in mono.resample(frame):
                        samples = array("h")
                        samples.frombytes(bytes(chunk.planes[0])[:chunk.samples * samples.itemsize])
                        audio.append(sum(map(abs, samples)) / len(samples))
                if not allow_audio.is_set() and len(frames) >= 5 and len(audio) >= 10:
                    assert max(audio) < 100
                    silence_frames = len(audio)
                    allow_audio.set()
                if len(frames) >= 45 and len(audio) >= 100 and max(audio) > 4000:
                    break
    except av.FFmpegError:
        raise RuntimeError('RTSP decode failed') from None
    assert len(frames) >= 45 and len(audio) >= 100 and len(seen) == 1
    assert silence_frames >= 10 and max(audio) > 4000
    # The RTSP receiver may align its clock at the first RTCP sender report.
    # The encoder witness checks exact capture timestamps before RTP.
    rebased = sum(abs(step - .06) > .001 for step in video_steps)
    assert rebased <= 1, video_steps
    return dict(decoded_video_frames=len(frames), exact_pixel_annotation_matches=len(frames),
                decoded_opus_frames=len(audio), distinct_annotation_epochs=len(seen),
                decoded_silent_opus_frames_before_camera_ack=silence_frames,
                rtsp_timing_steps_outside_1ms=rebased)

async def run(args, root):
    with ExitStack() as processes:
        gateway, _, _, port = start_media(processes, root, args)
        camera_root = root / 'camera'
        camera_root.mkdir(mode=0o700)
        cred = Credentials(camera_root, site='lab', gateway='gw')
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_cert_chain(camera_root/'server.pem', camera_root/'server.key')
        ctx.load_verify_locations(camera_root/'ca.pem')
        frames = [jpeg(args.ffmpeg, color) for color in ('red', 'blue')]
        client = ssl.PEM_cert_to_DER_cert((camera_root/'client.pem').read_text())
        received = []
        errors = []
        pending = set()
        allow_audio = threading.Event()
        async def accept(reader, writer):
            task = asyncio.current_task()
            pending.add(task)
            try:
                await camera(reader, writer, client, frames, received, allow_audio)
            except Exception as error:
                errors.append(type(error).__name__)
            finally:
                pending.remove(task)
        server = await asyncio.start_server(accept, '127.0.0.1', 0, ssl=ctx, limit=2048)
        async with server:
            publisher_file = root/'publisher.token'
            private(publisher_file, gateway.publisher)
            config = root/'ingest.json'
            private(config, json.dumps(dict(
                camera=f'127.0.0.1:{server.sockets[0].getsockname()[1]}', camera_audio=True,
                allow_insecure_camera_http=False, camera_tls=cred.config(),
                detections=dict(control=f'127.0.0.1:{gateway.internal}', camera_id='cam'),
                rtsp=f'127.0.0.1:{port}', stream_path='cam', publisher_user='publisher',
                publisher_token_file=str(publisher_file), ffmpeg=args.ffmpeg,
                publisher=str(args.publisher.resolve()), width=320, height=240, frame_timeout_ms=5000,
            )))
            ingest = start(processes, root, 'ingest', [args.ingest.resolve(), config, '--require-camera-tls'])
            deadline = time.monotonic() + 12
            while not gateway.path().get('ready'):
                assert ingest.poll() is None and not errors and time.monotonic() < deadline, errors
                await asyncio.sleep(.08)
            result = await asyncio.to_thread(receive, port, gateway.credentials['reader'].read_text(), allow_audio)
            assert received == [True] and not errors
            ingest.terminate()
            assert await asyncio.to_thread(ingest.wait, 8) == 0
            deadline = time.monotonic() + 4
            while gateway.path().get('ready'):
                assert time.monotonic() < deadline
                await asyncio.sleep(.05)
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending), 5)
            result['authenticated_camera_connections'] = len(received)
            result['publication_retired_on_stop'] = True
            print(json.dumps(result))

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('gateway', 'ingest', 'publisher', 'mediamtx'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--ffmpeg', default='/usr/bin/ffmpeg')
    args = p.parse_args()
    with tempfile.TemporaryDirectory(prefix='nxrs-matter-av-') as tmp:
        root = Path(tmp)
        asyncio.run(run(args, root))
