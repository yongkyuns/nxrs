#!/usr/bin/env python3
"""Exercise mute and live privacy through real Matter writes, media, and a process restart."""

import argparse
import asyncio
from array import array
from collections import deque
from contextlib import ExitStack
import json
from pathlib import Path
import socket

import av

from matter_audio_allocation import CLUSTER, LIVE_OPUS, rejected
from matter_audio_reference import publish_av
from matter_fabric_reference import attribute, controller
from matter_reference import measured_peer
from matter_rtsp_reference import ports, start_device, start_media, wait_path

MUTED, LIVE_PRIVATE, RECORD_PRIVATE = 29, 20, 19


class Samples:
    def __init__(self):
        self.frames = 0
        self.latest = deque(maxlen=200)
        self.stamp = None

    async def consume(self, track):
        convert = av.AudioResampler(format="s16", layout="mono", rate=16000)
        while True:
            frame = await track.recv()
            assert frame.sample_rate == 48000 and (self.stamp is None or frame.pts > self.stamp)
            self.stamp = frame.pts
            for chunk in convert.resample(frame):
                pcm = array("h")
                pcm.frombytes(bytes(chunk.planes[0])[: chunk.samples * pcm.itemsize])
                self.frames += 1
                hz = sum(a <= 0 < b for a, b in zip(pcm, pcm[1:])) * 16000 / len(pcm)
                self.latest.append((self.frames, max(abs(n) for n in pcm), hz))

    async def silence(self):
        # Require two continuous seconds of newly decoded exact-zero PCM after
        # allowing packets already in flight or in the decoder to drain.
        start = self.frames
        async with asyncio.timeout(5):
            while True:
                window = list(self.latest)[-100:]
                if len(window) == 100 and window[0][0] > start and all(p == 0 for _, p, _ in window):
                    return len(window)
                await asyncio.sleep(0.04)

    async def tone(self):
        start = self.frames
        async with asyncio.timeout(5):
            while not any(n > start and peak > 6000 and 950 <= hz <= 1050 for n, peak, hz in self.latest):
                await asyncio.sleep(0.04)


async def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    state = root / "state"
    with socket.socket(type=socket.SOCK_DGRAM) as check:
        check.bind((args.ip, 5540))
    with ExitStack() as stack:
        gateway, _, _, port = start_media(stack, root, args)

        def boot(name, initialize=False):
            runtime = root / name
            runtime.mkdir(mode=0o700)
            return start_device(stack, runtime, args, gateway, port, state, initialize=initialize)

        device = boot("first", True)
        children = publish_av(stack, root, args, port, gateway.publisher)
        path = wait_path(gateway, lambda p: p.get("ready") and len(p["readers"]) == 1, (*children, device))
        original_source = path["source"]["id"]
        async with controller(stack, root, args, "admin", ports(1)[0], 32704) as admin:
            paired = await admin.request(
                "commission_on_network", setup_pin_code=20202021, ip_addr=args.ip, discovery_timeout_s=60
            )
            node = paired["node_id"]

            async def read(attr):
                return await attribute(admin, node, f"1/{CLUSTER}/{attr}")

            async def write(attr, value):
                await admin.request(
                    "write_attribute", node_id=node, attribute_path=f"1/{CLUSTER}/{attr}", value=value
                )
                assert await read(attr) is value

            async def current():
                return await attribute(admin, node, "1/1363/0")

            async def ready():
                async with asyncio.timeout(60):
                    while True:
                        try:
                            alloc = await admin.request(
                                "device_command",
                                node_id=node,
                                endpoint_id=1,
                                cluster_id=CLUSTER,
                                command_name="AudioStreamAllocate",
                                payload=LIVE_OPUS,
                            )
                            return alloc["audioStreamID"]
                        except RuntimeError:
                            await asyncio.sleep(0.25)

            async def open_view():
                await admin.request(
                    "camera_start_stream",
                    node_id=node,
                    endpoint_id=1,
                    stream_usage="LiveView",
                    audio={},
                    allow_eviction=False,
                    client_session_key="privacy-check",
                )

            async def solicit(video_id):
                return await admin.request(
                    "device_command",
                    node_id=node,
                    endpoint_id=1,
                    cluster_id=0x553,
                    command_name="SolicitOffer",
                    payload=dict(streamUsage=3, originatingEndpointID=1, videoStreams=[video_id]),
                )

            async def denied_by_device(video_id):
                try:
                    await solicit(video_id)
                except RuntimeError as error:
                    if "(Matter status 203)" in str(error):
                        return
                    raise
                raise AssertionError("private camera admitted a direct offer")

            async def allocations():
                return [await read(15), await read(16)]

            async def restart(process, name):
                process.kill()
                process.wait(timeout=8)
                replacement = boot(name)
                async with asyncio.timeout(60):
                    while True:
                        try:
                            await read(MUTED)
                            return replacement
                        except RuntimeError:
                            await asyncio.sleep(0.25)

            await ready()
            assert (
                await read(MUTED) is True
                and await read(LIVE_PRIVATE) is False
                and await read(RECORD_PRIVATE) is False
            )
            samples = Samples()
            ended = asyncio.get_running_loop().create_future()
            async with measured_peer(
                admin,
                node,
                1,
                "receiver-offer",
                30,
                audio_observer=samples.consume,
                end_reason=ended,
                request_end=False,
            ) as (_, peer, video):
                initial = await samples.silence()
                await write(MUTED, False)
                await samples.tone()
                await write(RECORD_PRIVATE, True)
                await samples.tone()  # Recording privacy leaves LiveView available.

                async def packets():
                    return {
                        s.kind: s.packetsReceived
                        for s in (await peer.getStats()).values()
                        if s.type == "inbound-rtp"
                    }

                await write(MUTED, True)
                before = await packets()
                muted = await samples.silence()
                after = await packets()
                assert after["video"] > before["video"] and after["audio"] > before["audio"]
                async with asyncio.timeout(4):
                    stamps = [(await video.recv()).pts for _ in range(36)]
                assert all(a < b for a, b in zip(stamps, stamps[1:]))
                await write(MUTED, False)
                await samples.tone()
                await write(LIVE_PRIVATE, True)
                reason = await asyncio.wait_for(ended, 10)
                assert reason == 11, "device did not retire the session with PrivacyMode"
                async with asyncio.timeout(10):
                    while await current():
                        await asyncio.sleep(0.1)
                await asyncio.sleep(0.5)
                stopped = await packets()
                await asyncio.sleep(1)
                assert set(stopped) == {"audio", "video"} and await packets() == stopped
            await rejected(open_view)
            assert not await current()
            await write(MUTED, True)
            expected = await allocations()
            video_id = expected[0][0]["0"]
            await denied_by_device(video_id)
            device = await restart(device, "restarted")
            assert [await read(i) for i in (MUTED, LIVE_PRIVATE, RECORD_PRIVATE)] == [True, True, True]
            assert await allocations() == expected
            await rejected(open_view)
            await denied_by_device(video_id)
            assert not await current()
            await write(LIVE_PRIVATE, False)
            await ready()
            restored = Samples()
            async with measured_peer(admin, node, 1, "camera-offer", 30, audio_observer=restored.consume):
                persisted = await restored.silence()
                await write(MUTED, False)
                await restored.tone()
            expected = await allocations()
            held_id = expected[0][0]["0"]
            await solicit(held_id)
            held = next(row for row in await read(15) if row["0"] == held_id)
            assert held["12"] > 0 and len(await current()) == 1
            await restart(device, "unmuted-restart")
            assert not await current()
            assert [await read(i) for i in (MUTED, LIVE_PRIVATE, RECORD_PRIVATE)] == [False, False, True]
            assert await allocations() == expected
            await ready()
            audible = Samples()
            async with measured_peer(admin, node, 1, "receiver-offer", 30, audio_observer=audible.consume):
                await audible.tone()
            video_id = (await read(15))[0]["0"]
            await solicit(video_id)
            assert len(await current()) == 1
            await write(LIVE_PRIVATE, True)
            await write(LIVE_PRIVATE, False)
            async with asyncio.timeout(8):
                while await current():
                    await asyncio.sleep(0.1)
            assert gateway.path()["source"]["id"] == original_source and all(
                p.poll() is None for p in children
            )
            return dict(
                initial_zero_pcm_frames=initial,
                live_muted_zero_pcm_frames=muted,
                muted_video_frames=len(stamps),
                unmute_recovered_tone=True,
                privacy_end_reason=reason,
                active_audio_and_video_stopped=True,
                blocked_before_and_after_restart=True,
                restarted_with_same_enrollment=True,
                restarted_zero_pcm_frames=persisted,
                recording_privacy_independent=True,
                publisher_continued=True,
                unmuted_tone_after_second_restart=True,
                allocations_preserved_across_restarts=True,
                direct_solicit_denied_while_private=True,
                pending_offer_retired=True,
                reboot_cleared_session_references=True,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "device", "controller", "gateway", "publisher", "mediamtx"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--ip", required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    (args.runtime / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
