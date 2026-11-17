#!/usr/bin/env python3
"""Commission the control fixture and check real Matter microphone allocation commands."""
import argparse
import asyncio
from array import array
from contextlib import AsyncExitStack, ExitStack
import json
from pathlib import Path
import socket
import time
import statistics
import av

from matter_audio_reference import publish_av
from matter_fabric_reference import attribute, controller
from matter_reference import measured_peer
from matter_rtsp_reference import ports, publish, start_device, start_media, wait_path

CLUSTER = 0x551
LIVE_OPUS = dict(streamUsage=3, audioCodec=0, channelCount=1, sampleRate=16000, bitRate=64000, bitDepth=16)


async def rejected(command, **payload):
    try:
        await command(**payload)
    except RuntimeError as error:
        if "controller error " in str(error):
            return
        raise
    raise AssertionError("controller unexpectedly acknowledged the command")


async def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    with socket.socket(type=socket.SOCK_DGRAM) as probe:
        probe.bind((args.ip, 5540))
    with ExitStack() as stack:
        gateway, _, _, rtsp = start_media(stack, root, args)
        device = start_device(stack, root, args, gateway, rtsp, root / "device-state", initialize=True)
        async with controller(stack, root, args, "admin", ports(1)[0], 32704) as admin:
            paired = await admin.request("commission_on_network", setup_pin_code=20202021, ip_addr=args.ip,
                                         discovery_timeout_s=60)
            node = paired["node_id"]

            async def cmd(name, payload):
                return await admin.request("device_command", node_id=node, endpoint_id=1, cluster_id=CLUSTER,
                                           command_name=name, payload=payload)

            async def allocate(**changes):
                return await cmd("AudioStreamAllocate", {**LIVE_OPUS, **changes})

            async def deallocate(stream):
                return await cmd("AudioStreamDeallocate", dict(audioStreamID=stream))

            async def rows():
                return await attribute(admin, node, f"1/{CLUSTER}/16")

            async def wait_for_allocation():
                until = time.monotonic() + 28  # Paired input waits for both RTCP reports and an IDR.
                while True:
                    try:
                        return await allocate()
                    except RuntimeError:
                        if time.monotonic() >= until:
                            raise
                        await asyncio.sleep(.15)

            feature = await attribute(admin, node, f"1/{CLUSTER}/65532")
            capabilities = await attribute(admin, node, f"1/{CLUSTER}/7")
            assert capabilities == {"0": 1, "1": [0], "2": [16000], "3": [16]}
            accepted = await attribute(admin, node, f"1/{CLUSTER}/65529")
            assert 0 in accepted and 2 in accepted
            assert not await rows()
            await rejected(allocate)  # No publisher.
            with ExitStack() as av_source:
                children = publish_av(av_source, root, args, rtsp, gateway.publisher)
                wait_path(gateway, lambda p: p.get("ready") and len(p["readers"]) == 1, (*children, device))
                reply = await wait_for_allocation()
                stream = reply["audioStreamID"]
                assert await allocate() == reply
                listed = await rows()
                assert len(listed) == 1 and listed[0] == {
                    "0": stream, "1": 3, "2": 0, "3": 1, "4": 16000, "5": 64000, "6": 16, "7": 0}
                for changes in (dict(channelCount=2), dict(audioCodec=1), dict(bitRate=32000), dict(streamUsage=0)):
                    await rejected(allocate, **changes)
                assert await rows() == listed
                playback = []
                if args.playback:
                    for flow in ("receiver-offer", "camera-offer"):
                        done = asyncio.get_running_loop().create_future()
                        async def observe(track):
                            try:
                                done.set_result(await measure_tone(track))
                            except Exception as error:
                                done.set_exception(error)
                        async with measured_peer(admin, node, 1, flow, 60, audio_observer=observe) as (report, _, _):
                            tone = await asyncio.wait_for(done, 12)
                            active_rows = await rows()
                            assert len(active_rows) == 1 and active_rows[0]["7"] == 1
                            await rejected(deallocate, stream=stream)
                            assert len(gateway.path()["readers"]) == 1
                            playback.append(dict(flow=flow, video_frames=report["decoded_frames"], **tone))
                        async with asyncio.timeout(6):
                            while (value := await rows()) and value[0]["7"] != 0:
                                await asyncio.sleep(.1)
                        # Matter Server may release its own allocation at stop.
                        stream = (await allocate())["audioStreamID"]
                await deallocate(stream)
                assert not await rows()
                await rejected(deallocate, stream=stream)
                replacement = (await allocate())["audioStreamID"]
                assert replacement != stream
            wait_path(gateway, lambda p: not p.get("ready") and not p.get("readers"))
            with ExitStack() as video:
                children = publish(video, root, args, rtsp, gateway.publisher, "blue", "320x240", 10)
                wait_path(gateway, lambda p: p.get("ready") and len(p["readers"]) == 1, (*children, device))
                await rejected(allocate)
                await deallocate(replacement)
                assert not await rows()
            wait_path(gateway, lambda p: not p.get("ready") and not p.get("readers"))
            with ExitStack() as revoked:
                children = publish_av(revoked, root, args, rtsp, gateway.publisher)
                before = wait_path(gateway, lambda p: p.get("ready") and len(p["readers"]) == 1, (*children, device))
                active = (await wait_for_allocation())["audioStreamID"]
                async with AsyncExitStack() as peers:
                    if args.playback:
                        tone = asyncio.get_running_loop().create_future()
                        async def observe_revoked(track):
                            try:
                                tone.set_result(await measure_tone(track))
                            except Exception as error:
                                tone.set_exception(error)
                        _, peer, _ = await peers.enter_async_context(measured_peer(
                            admin, node, 1, "receiver-offer", 60, request_end=False, audio_observer=observe_revoked))
                        await asyncio.wait_for(tone, 12)
                        assert (await rows())[0]["7"] == 1
                    gateway.revoke()
                    after = wait_path(gateway, lambda p: p.get("ready") and not p["readers"], (*children, device))
                    if args.playback:
                        async with asyncio.timeout(10):
                            while await attribute(admin, node, "1/1363/0") or (await rows())[0]["7"]:
                                await asyncio.sleep(.1)
                        async def counters():
                            return {x.kind: x.packetsReceived for x in (await peer.getStats()).values()
                                    if x.type == "inbound-rtp"}
                        await asyncio.sleep(.5)
                        stopped = await counters()
                        assert set(stopped) == {"audio", "video"} and min(stopped.values()) > 0
                        await asyncio.sleep(1)
                        assert await counters() == stopped, "media continued after revocation"
                assert after["source"]["id"] == before["source"]["id"]
                # Server-side removal may precede the bridge noticing EOF.
                async with asyncio.timeout(6):
                    while True:
                        try:
                            await allocate()
                        except RuntimeError as error:
                            if "controller error " not in str(error):
                                raise
                            break
                        await asyncio.sleep(.05)
                await deallocate(active)
                assert not await rows()
            return dict(**({"playback": playback, "active_revocation_stopped_both_tracks": True} if args.playback else {}), feature_map=feature, supported_sample_rates=capabilities["2"],
                        allocation_commands_present=True, duplicate_reused_id=True,
                        incompatible_requests_rejected=4, offline_and_video_only_denied=True,
                        revoked_source_denied_while_publishing=True, retired_id_rejected=True, final_allocations=0)


async def measure_tone(track):
    mono = av.AudioResampler(format="s16", layout="mono", rate=16000)
    tones, pulses, frames, last_active, previous = [], 0, 0, None, None
    async with asyncio.timeout(14):
        while pulses < 5:
            frame = await track.recv()
            assert frame.sample_rate == 48000 and (previous is None or frame.pts > previous)
            previous = frame.pts
            frames += 1
            for chunk in mono.resample(frame):
                pcm = array("h")
                pcm.frombytes(bytes(chunk.planes[0])[:chunk.samples * pcm.itemsize])
                active = sum(abs(s) for s in pcm) / len(pcm) > 3000
                if active:
                    tones.append(sum(a <= 0 < b for a,b in zip(pcm, pcm[1:])) * 16000 / len(pcm))
                    pulses += last_active is False
                last_active = active
    hz = statistics.median(tones)
    assert 950 <= hz <= 1050
    return dict(decoded_audio_frames=frames, pulses=pulses, median_hz=round(hz, 1))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "gateway", "mediamtx", "publisher", "device", "controller"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--ip", required=True)
    parser.add_argument("--playback", action="store_true", help="exercise both Matter audio/video offer directions")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    (args.runtime / "result.json").write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps(result))
