#!/usr/bin/env python3
"""Verify controlled A/V ingest with an isolated mTLS camera fixture."""
import argparse
from array import array
import asyncio
from contextlib import ExitStack, suppress
from copy import deepcopy
import json
import math
import re
from pathlib import Path
import ssl
import struct
import subprocess
import sys
import tempfile
import time

import aiohttp
import av
from matter_attestation_reference import public_test_bundle
from matter_commissioning_reference import provision
from matter_fabric_reference import attribute, controller
from matter_privacy_reference import Samples
from matter_reference import measured_peer
from matter_rtsp_reference import ports, start_device, start_media
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

async def camera(reader, writer, cert, frames, received, commands):
    read_command = None
    try:
        peer = writer.get_extra_info('ssl_object').getpeercert(binary_form=True)
        assert peer == cert
        request = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 3)
        assert request.split(b'\r\n', 1)[0] == b'GET /stream?av=2 HTTP/1.1'
        received.append(True)
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=nxrs-frame\r\nX-Nxrs-AV: 2\r\nConnection: close\r\n\r\n')
        await writer.drain()
        read_command = asyncio.create_task(reader.readexactly(9))
        first_audio = None
        sequence = 1
        tone = struct.pack('<320h', *(round(18_000 * math.sin(i * math.pi / 8)) for i in range(SAMPLES)))
        template = json.loads((ROOT / 'clients/ios/Tests/NxrsMonitorCoreTests/Fixtures/detection.json').read_text())['source']
        began = time.monotonic()
        for index in range(6000):
            stamp = CLOCK_START + index * 20
            seq = index // 3 + 1
            if read_command.done():
                command = read_command.result()
                word, muted = struct.unpack('>QB', command)
                assert word == sequence and muted in (0, 1)
                assert bool(muted) == (first_audio is not None)
                commands.put_nowait(bool(muted))
                writer.write(part('application/vnd.nxrs.microphone-state.v2', command, ''))
                first_audio = None if muted else index
                sequence += 1
                read_command = asyncio.create_task(reader.readexactly(9))
            if first_audio is not None:
                writer.write(part('application/vnd.nxrs.pcm16.v1', tone,
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
    except (BrokenPipeError, ConnectionResetError, asyncio.IncompleteReadError):
        pass
    finally:
        if read_command is not None:
            read_command.cancel()
            with suppress(asyncio.CancelledError, OSError, asyncio.IncompleteReadError):
                await read_command
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

def receive(port, token, report):
    # Suppress SDK logs and exceptions that could include the temporary RTSP token.
    av.logging.set_level(av.logging.PANIC)
    url = f'rtsp://reader:{token}@127.0.0.1:{port}/cam'
    frames = []
    audio = 0
    expected = iter((True, False, True, False, True))
    muted = next(expected)
    consecutive = 0
    last_reported_sequence = 0
    transition_frames = []
    mono = av.AudioResampler(format="s16", layout="mono", rate=16_000)
    seen = set()
    video_steps = []
    startup_frames_without_pts = 0
    try:
        with av.open(url, options={'rtsp_transport': 'tcp'}, timeout=(4, 5)) as streams:
            assert [(s.type, s.codec_context.name) for s in streams.streams] == [('video', 'h264'), ('audio', 'opus')]
            picture = av.CodecContext.create('h264', 'r')
            picture.thread_count = 1
            picture.extradata = streams.streams.video[0].codec_context.extradata
            sps = [nal for nal in re.split(b'\x00\x00\x00?\x01', picture.extradata or b'') if nal and nal[0] & 31 == 7]
            assert len(sps) == 1 and len(sps[0]) >= 4 and sps[0][3] <= 31, '320×240 A/V must fit H.264 Level 3.1'
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
            end = time.monotonic() + 25
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
                    if frame.pts is None:
                        # Validate the image and annotation even before the
                        # RTSP timestamp is established. Start timing at the
                        # first stamped picture and reject later gaps.
                        assert not frames and startup_frames_without_pts == 0
                        startup_frames_without_pts += 1
                        continue
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
                        audio += 1
                        amplitude = sum(map(abs, samples)) / len(samples)
                        matches = amplitude < 100 if muted else amplitude > 4000
                        if matches:
                            consecutive += 1
                        else:
                            consecutive = 0
                    if consecutive >= 14 and len(frames) >= last_reported_sequence + 4:
                        transition_frames.append(len(frames))
                        last_reported_sequence = len(frames)
                        report(muted)
                        muted = next(expected, None)
                        if muted is None:
                            break
                        consecutive = 0
    except av.FFmpegError:
        raise RuntimeError('RTSP decode failed') from None
    assert len(frames) >= 20 and audio >= 70 and len(seen) == 1 and len(transition_frames) == 5
    # The RTSP receiver may align its clock at the first RTCP sender report.
    # The encoder witness checks exact capture timestamps before RTP.
    rebased = sum(abs(step - .06) > .001 for step in video_steps)
    assert rebased <= 1, video_steps
    return dict(decoded_video_frames=len(frames), exact_pixel_annotation_matches=len(frames),
                decoded_opus_frames=audio, distinct_annotation_epochs=len(seen),
                video_frame_at_each_audio_transition=transition_frames,
                startup_frames_without_pts=startup_frames_without_pts,
                rtsp_timing_steps_outside_1ms=rebased)

def watch_media(tasks, gateway, port):
    phases = asyncio.Queue(maxsize=5)
    loop = asyncio.get_running_loop()
    listen = tasks.create_task(asyncio.to_thread(
        receive, port, gateway.credentials['reader'].read_text(),
        lambda muted: loop.call_soon_threadsafe(phases.put_nowait, muted),
    ))
    return phases, listen

async def expect(queue, muted, deadline=5):
    assert await asyncio.wait_for(queue.get(), deadline) is muted

async def verify_gateway(gateway, stage, port, commands):
    url = f'http://127.0.0.1:{gateway.internal}/internal/cameras/cam/microphone'
    headers = {'Authorization': 'Bearer ' + (stage/'microphone.token').read_text().strip()}
    async with (
        aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=2)) as session,
        asyncio.TaskGroup() as tasks,
    ):
        async def publish(permit):
            async with session.post(url, json={'permit': permit}) as response:
                assert response.status == 204, response.status

        async def renew(permit):
            while True:
                await publish(permit)
                await asyncio.sleep(.4)

        phases, listen = watch_media(tasks, gateway, port)
        await expect(phases, True)
        assert commands.empty()
        for generation in (1, 2):
            refresh = tasks.create_task(renew([generation] * 16))
            await expect(commands, False)
            await expect(phases, False)
            refresh.cancel()
            with suppress(asyncio.CancelledError):
                await refresh
            if generation == 1:
                await publish(None)
            await expect(commands, True, 3)
            await expect(phases, True)
        result = await listen
    result['wire_mute_commands'] = [False, True, False, True]
    result['last_mute_cause'] = 'expired capture permit after stopping renewal'
    return result

async def verify_matter(args, root, processes, gateway, stage, port, commands):
    factory = provision(root / 'factory', args.controller)
    attestation = root / 'attestation'
    public_test_bundle(args.sdk, attestation)
    state = root / 'state'
    deployment = root / 'matter.json'
    private(deployment, json.dumps(dict(
        version=1, site_id='lab', camera_id='cam', control=f'127.0.0.1:{gateway.control}',
        rtsp=f'127.0.0.1:{port}', ice_address=args.ip, lan_interface=args.interface,
        media_token_file=str(stage/'media.token'), detection_token_file=str(stage/'detections.token'),
        factory_data=str(factory), state_directory=str(state), snapshot_ffmpeg=args.ffmpeg,
        attestation_directory=str(attestation), host_power_source='dc',
        microphone_control=dict(address=f'127.0.0.1:{gateway.internal}',
                                token_file=str(stage/'microphone.token')),
    )))

    bridge = start_device(processes, root, args, gateway, port, state, initialize=True,
                          extra_args=('--config', deployment, '--allow-test-attestation',
                                      '--open-commissioning-window'))
    controller_port = 15580 if args.browser_python else ports(1)[0]
    async with (
        controller(processes, root, args, 'admin', controller_port, 32704) as admin,
        asyncio.TaskGroup() as tasks,
    ):
        code = json.loads((factory.parent/'onboarding.json').read_bytes())['qrCode']
        paired = await admin.request('commission_with_code', code=code, network_only=True,
                                     discovery_timeout_s=30)
        node = paired['node_id']
        dimensions = {'0': 320, '1': 240}
        assert await attribute(admin, node, '1/1361/4') == dimensions
        rate, = await attribute(admin, node, '1/1361/5')
        assert rate['0'] == 0 and rate['1'] == dimensions
        stream, = await attribute(admin, node, '1/1361/15')
        assert stream['2'] == 0 and stream['5'] == dimensions
        muted_attribute = '1/1361/29'
        assert await attribute(admin, node, muted_attribute) is True
        browser = None
        if args.browser_python:
            browser_output = root / 'home-assistant-browser.json'
            browser_command = [
                args.browser_python, Path(__file__).with_name('matter_browser_reference.py'),
                '--entity', args.camera_entity, '--storage-state', args.storage_state,
                '--browser-engine', args.browser_engine, '--output', browser_output,
            ]
            if args.check_thumbnail:
                browser_command.append('--check-thumbnail')
            await asyncio.to_thread(
                subprocess.run, browser_command, stdout=subprocess.DEVNULL, check=True, timeout=95,
            )
            assert commands.empty(), 'opening the Home Assistant dialog started microphone capture'
            assert await attribute(admin, node, muted_attribute) is True
            async with asyncio.timeout(5):
                while await attribute(admin, node, '1/1363/0'):
                    await asyncio.sleep(.1)
            browser = json.loads(browser_output.read_bytes())
            candidates = browser['candidate_responses']
            assert candidates['accepted'].get('mdns', 0) > 0
            assert not candidates['rejected']
        phases, listen = watch_media(tasks, gateway, port)

        await expect(phases, True)
        assert commands.empty()
        samples = Samples()
        async with measured_peer(
            admin, node, 1, 'receiver-offer', 30, audio_observer=samples.consume, bundled=True,
        ) as (matter_video, peer, video):
            assert matter_video['trickle_ice']['sent'] > matter_video['distinct_sent_candidates']
            async def video_packets():
                return sum(stat.packetsReceived for stat in (await peer.getStats()).values()
                           if stat.type == 'inbound-rtp' and stat.kind == 'video')

            sessions = await attribute(admin, node, '1/1363/0')
            assert len(sessions) == 1
            quiet_frames = [await samples.silence()]
            previous_pts = -1
            phase_video = []
            phase_packets = []
            for muted in (False, True, False):
                before = await video_packets()
                await admin.request(
                    'write_attribute', node_id=node, attribute_path=muted_attribute, value=muted,
                )
                assert await attribute(admin, node, muted_attribute) is muted
                await expect(commands, muted)
                await expect(phases, muted)
                if muted:
                    quiet_frames.append(await samples.silence())
                else:
                    await samples.tone()
                async with asyncio.timeout(4):
                    pictures = [(await video.recv()).pts for _ in range(12)]
                assert previous_pts < pictures[0] and all(a < b for a, b in zip(pictures, pictures[1:]))
                previous_pts = pictures[-1]
                phase_video.append(len(pictures))
                advancing = await video_packets() - before
                assert advancing > 0
                phase_packets.append(advancing)
                assert await attribute(admin, node, '1/1363/0') == sessions
                assert peer.connectionState == 'connected'
        async with asyncio.timeout(5):
            while await attribute(admin, node, '1/1363/0'):
                await asyncio.sleep(.1)
        bridge.kill()
        assert await asyncio.to_thread(bridge.wait, 5) != 0
        await expect(commands, True, 3)
        await expect(phases, True)
        result = await listen
        matter_video.update(
            silent_decoded_opus_frames=quiet_frames,
            video_frames_per_capture_transition=phase_video,
            video_rtp_packets_during_capture_transition=phase_packets,
            decoded_audio_phases=['silent', 'tone', 'silent', 'tone'],
            teardown_acknowledged=True,
        )
        result['matter_webrtc'] = matter_video
        if browser:
            result['home_assistant_browser_while_capture_muted'] = browser
        result['wire_mute_commands'] = [False, True, False, True]
        result['last_mute_cause'] = 'expired capture permit after stopping Matter bridge'
    return result

async def run(args, root):
    stage = root / 'matter-enrollment'
    with ExitStack() as processes:
        def configure(gateway):
            subprocess.run([
                sys.executable, str(ROOT / 'gateway/tools/stage_matter.py'),
                str(gateway.config), str(stage), '--camera', 'cam', '--identity', 'bridge', '--microphone',
            ], check=True, stdout=subprocess.DEVNULL)
            gateway.config = stage / 'control.json'
        gateway, _, _, port = start_media(processes, root, args, configure=configure)
        commands = asyncio.Queue(maxsize=8)
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
        async def accept(reader, writer):
            task = asyncio.current_task()
            pending.add(task)
            try:
                await camera(reader, writer, client, frames, received, commands)
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
                microphone_control=f'127.0.0.1:{gateway.internal}',
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
            source_id = gateway.path()['source']['id']
            if args.gateway_only:
                result = await verify_gateway(gateway, stage, port, commands)
            else:
                result = await verify_matter(args, root, processes, gateway, stage, port, commands)
            assert commands.empty(), 'unexpected microphone capture commands'
            assert gateway.path()['source']['id'] == source_id
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
    for name in ('device', 'controller', 'sdk'):
        p.add_argument('--' + name, type=Path)
    p.add_argument('--gateway-only', action='store_true', help='drive capture permits directly through gateway control')
    p.add_argument('--ffmpeg', default='/usr/bin/ffmpeg')
    p.add_argument('--interface')
    p.add_argument('--ip')
    p.add_argument('--runtime', type=Path)
    p.add_argument('--browser-python', type=Path, help='the isolated Home Assistant browser Python')
    p.add_argument('--browser-engine', choices=('chrome', 'webkit'), default='chrome')
    p.add_argument('--check-thumbnail', action='store_true', help='verify the Home Assistant JPEG thumbnail')
    p.add_argument('--storage-state', type=Path, help='the isolated Home Assistant browser storage state')
    p.add_argument('--camera-entity', help='camera discovered by isolated Home Assistant on the fixture controller')
    args = p.parse_args()
    browser_inputs = args.browser_python, args.storage_state, args.camera_entity
    matter_inputs = args.device, args.controller, args.sdk, args.interface, args.ip
    if args.gateway_only and (any(matter_inputs) or any(browser_inputs) or args.check_thumbnail):
        p.error('--gateway-only cannot be combined with Matter or browser arguments')
    if not args.gateway_only and not all(matter_inputs):
        p.error('supply --device, --controller, --sdk, --interface and --ip for the Matter test')
    if any(browser_inputs) and not all(browser_inputs):
        p.error('set all three Home Assistant browser arguments together')
    if args.check_thumbnail and not args.browser_python:
        p.error('--check-thumbnail requires the Home Assistant browser arguments')
    if args.runtime:
        root = args.runtime.resolve()
        root.mkdir(mode=0o700)
        asyncio.run(run(args, root))
    else:
        with tempfile.TemporaryDirectory(prefix='nxrs-matter-av-') as tmp:
            asyncio.run(run(args, Path(tmp)))
