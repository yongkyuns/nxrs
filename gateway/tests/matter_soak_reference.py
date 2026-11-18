#!/usr/bin/env python3
"""Sustain four real Matter/WebRTC receivers against an isolated Linux bridge."""

import argparse
import asyncio
from contextlib import AsyncExitStack, ExitStack
import json
import os
from pathlib import Path
import pwd
import re
import sys
import time

from matter_commissioning_reference import provision
from matter_fabric_reference import attribute, controller, packets
from matter_gateway_reference import start, stop
from matter_lan_reference import PEER_LINK, SELECTED, network
from matter_reference import measured_peer
from matter_rtsp_reference import ports, publish, start_media, wait_path
from matter_session_reference import selected_port
from matter_source_access import private_file

VIEWERS = 4
SESSIONS = "1/1363/0"
VIDEO = "1/1361/15"
SOURCE_FPS = 15


class Viewer:
    def __init__(self, client, node, controller_lifetime):
        self.client = client
        self.node = node
        self.controller_lifetime = controller_lifetime
        self.frames = 0
        self.last = None
        self.max_gap = 0
        self.drain = None
        self.peer = None
        self.media = AsyncExitStack()

    async def command(self, **args):
        return await self.client.request("device_command", node_id=self.node, **args)

    async def sessions(self):
        result = await self.client.request(
            "read_attribute", node_id=self.node, attribute_path=SESSIONS, fabric_filtered=True
        )
        return result[SESSIONS]

    async def offer(self):
        streams = await attribute(self.client, self.node, VIDEO)
        return await self.command(
            endpoint_id=1,
            cluster_id=0x553,
            command_name="SolicitOffer",
            payload=dict(streamUsage=3, originatingEndpointID=1, videoStreams=[streams[0]["0"]]),
        )

    async def open(self):
        begin = time.monotonic()
        async with asyncio.timeout(40):
            while True:
                try:
                    media, peer, track = await self.media.enter_async_context(
                        measured_peer(self.client, self.node, 1, "receiver-offer", 12, request_end=False)
                    )
                    break
                except RuntimeError as error:
                    if all(f"(Matter status {status})" not in str(error) for status in (156, 203)):
                        raise
                    await asyncio.sleep(0.2)
        selected_port(peer, SELECTED[2])
        self.peer = peer
        self.frames += media["decoded_frames"]
        self.last = time.monotonic()
        self.max_gap = 0
        self.drain = asyncio.create_task(self.consume(track))
        return round(self.last - begin, 3)

    async def consume(self, track):
        stamp = None
        while True:
            frame = await track.recv()
            assert (frame.width, frame.height) == (640, 360)
            assert stamp is None or frame.pts > stamp
            now = time.monotonic()
            gap = now - self.last
            if gap >= 0.5 and stamp is not None:
                media_gap = float((frame.pts - stamp) * frame.time_base)
                print(f"Decoded frame gap {gap:.3f}s, media timestamp advanced {media_gap:.3f}s", flush=True)
            stamp = frame.pts
            self.max_gap = max(self.max_gap, gap)
            self.last = now
            self.frames += 1

    def sample(self):
        if self.drain.done():
            self.drain.result()
            raise AssertionError("viewer stopped")
        age = time.monotonic() - self.last
        assert age < 3, f"viewer frame age {age:.3f} seconds"
        result = dict(frames=self.frames, longest_gap_seconds=round(self.max_gap, 3))
        self.max_gap = 0
        return result

    async def close(self, *, end=False):
        if end:
            for session in await self.sessions():
                await self.command(
                    endpoint_id=1,
                    cluster_id=0x553,
                    command_name="EndSession",
                    payload=dict(webRtcSessionId=session["0"], reason=2),
                )
        if self.drain is not None:
            self.drain.cancel()
            await asyncio.gather(self.drain, return_exceptions=True)
            self.drain = None
        await self.media.aclose()
        self.peer = None


async def serve_viewers(args, reader, writer):
    args.interface = PEER_LINK
    root = args.runtime
    ticket = json.loads((root / "factory/onboarding.json").read_bytes())
    viewers = []
    async with AsyncExitStack() as stack:

        async def respond(result):
            writer.write((json.dumps(result) + "\n").encode())
            await writer.drain()

        async def add(index):
            # A replaced controller owns its own storage, fabric and process.
            lifetime = await stack.enter_async_context(AsyncExitStack())
            resources = ExitStack()
            lifetime.push_async_callback(asyncio.to_thread, resources.close)
            session = await lifetime.enter_async_context(
                controller(resources, root, args, f"controller-{index}", ports(1)[0], 32800 + index)
            )
            try:
                async with asyncio.timeout(45):
                    if index == 0:
                        result = await session.request(
                            "commission_with_code",
                            code=ticket["qrCode"],
                            network_only=True,
                            discovery_timeout_s=30,
                        )
                    else:
                        owner = viewers[0]
                        invite = await owner.client.request(
                            "open_commissioning_window",
                            node_id=owner.node,
                            timeout=180,
                            iteration=1000,
                            option=1,
                        )
                        result = await session.request(
                            "commission_on_network",
                            setup_pin_code=invite["setup_pin_code"],
                            ip_addr=SELECTED[3],
                            discovery_timeout_s=30,
                        )
            except TimeoutError as error:
                raise RuntimeError(f"controller {index} commissioning timed out") from error
            viewer = Viewer(session, result["node_id"], lifetime)
            lifetime.push_async_callback(viewer.close)
            return viewer

        async def require_four_sessions():
            rows = await viewers[0].client.request(
                "read_attribute", node_id=viewers[0].node, attribute_path=SESSIONS, fabric_filtered=False
            )
            assert len(rows[SESSIONS]) == VIEWERS
            assert (
                sum(row["12"] for row in await attribute(viewers[0].client, viewers[0].node, VIDEO))
                == VIEWERS
            )

        async def open_all():
            # Start each controller's negotiation after the previous one is
            # decoding. The four sessions then share the sustained workload.
            async with asyncio.timeout(40):
                times = [await viewer.open() for viewer in viewers]
            await require_four_sessions()
            return dict(startup_seconds=times)

        async def full():
            try:
                await viewers[0].offer()
            except RuntimeError as error:
                if "(Matter status 137)" not in str(error):
                    raise
            else:
                raise AssertionError("a fifth peer was admitted")
            return dict(fifth_offer_status=137)

        async def slow_offer():
            slow = viewers[-1]
            await slow.close(end=True)
            offer = await slow.offer()
            start = time.monotonic()
            assert [row["0"] for row in await slow.sessions()] == [offer["webRtcSessionId"]]
            before = [peer.frames for peer in viewers[:-1]]
            async with asyncio.timeout(25):
                while await slow.sessions():
                    await asyncio.sleep(0.2)
            elapsed = time.monotonic() - start
            assert elapsed >= 17
            assert all(viewer.frames - old > 100 for viewer, old in zip(viewers[:-1], before))
            assert max(viewer.max_gap for viewer in viewers[:-1]) < 1
            return dict(
                offer_retired_seconds=round(elapsed, 3), replacement_startup_seconds=await slow.open()
            )

        async def restart():
            await asyncio.gather(*(viewer.close() for viewer in viewers))
            owner = viewers[0]
            assert await attribute(owner.client, owner.node, "0/62/3") == VIEWERS
            assert await attribute(owner.client, owner.node, "0/60/0") == 0
            return await open_all()

        async def revoke():
            owner, retired = viewers[0], viewers[-1]
            started = time.monotonic()

            def record(stage):
                gaps = [round(viewer.max_gap, 3) for viewer in viewers[:-1]]
                print(
                    f"Revocation {stage} after {time.monotonic() - started:.3f}s, surviving gaps {gaps}",
                    flush=True,
                )

            index = await attribute(retired.client, retired.node, "0/62/5")
            result = await owner.command(
                endpoint_id=0, cluster_id=62, command_name="RemoveFabric", payload={"fabricIndex": index}
            )
            assert result["statusCode"] == 0
            record("acknowledged")
            async with asyncio.timeout(5):
                while True:
                    rows = await owner.client.request(
                        "read_attribute", node_id=owner.node, attribute_path=SESSIONS, fabric_filtered=False
                    )
                    if len(rows[SESSIONS]) == VIEWERS - 1:
                        break
                    await asyncio.sleep(0.1)
            await asyncio.sleep(2)  # Leave the peer connected while its existing RTP drains.
            before = [viewer.frames for viewer in viewers[:-1]]
            retired_frames = retired.frames
            retired_packets = await packets(retired.peer)
            assert retired_packets > 0
            await asyncio.sleep(4)
            surviving_frames = [viewer.frames - old for viewer, old in zip(viewers[:-1], before)]
            assert all(count >= 30 for count in surviving_frames)
            assert retired.frames == retired_frames and await packets(retired.peer) == retired_packets
            record("traffic checked")
            await retired.controller_lifetime.aclose()
            record("old controller stopped")
            commission_start = time.monotonic()
            replacement = await add(VIEWERS)
            commissioning = round(time.monotonic() - commission_start, 3)
            record("new controller commissioned")
            viewers[-1] = replacement
            startup = await replacement.open()
            await require_four_sessions()
            record("sessions restored")
            now = time.monotonic()
            max_surviving_gap = max(max(viewer.max_gap, now - viewer.last) for viewer in viewers[:-1])
            assert max_surviving_gap < 1, f"surviving viewer frame gap {max_surviving_gap:.3f}s"
            return dict(
                revoked_fabric_replaced=True,
                removed_rtp_packets=0,
                surviving_frames=surviving_frames,
                longest_surviving_gap_seconds=round(max_surviving_gap, 3),
                replacement_commission_seconds=commissioning,
                replacement_startup_seconds=startup,
            )

        for index in range(VIEWERS):
            viewers.append(await add(index))
        await respond(await open_all())
        actions = {"full": full, "slow_offer": slow_offer, "restart": restart, "revoke": revoke}
        while line := await reader.readline():
            command = line.decode().strip()
            if command == "sample":
                result = [viewer.sample() for viewer in viewers]
            elif command == "finish":
                await asyncio.gather(*(viewer.close(end=True) for viewer in viewers))
                assert not any(await asyncio.gather(*(viewer.sessions() for viewer in viewers)))
                owner = viewers[0]
                assert not sum(row["12"] for row in await attribute(owner.client, owner.node, VIDEO))
                await respond({"closed": True})
                return
            else:
                result = await actions[command]()
            await respond(result)


async def peer_main(args):
    connected = asyncio.get_running_loop().create_future()

    def accept(reader, writer):
        if connected.done():
            writer.close()
        else:
            connected.set_result((reader, writer))

    listener = await asyncio.start_unix_server(accept, path=args.runtime / "viewers.sock")
    async with listener:
        reader, writer = await asyncio.wait_for(connected, 20)
        try:
            await serve_viewers(args, reader, writer)
        finally:
            writer.close()
            await writer.wait_closed()


def resources(process):
    status = Path(f"/proc/{process.pid}/status").read_text()
    assert process.poll() is None
    (rss,) = re.findall(r"^VmRSS:\s+(\d+) kB", status, re.MULTILINE)
    (threads,) = re.findall(r"^Threads:\s+(\d+)", status, re.MULTILINE)
    return dict(
        rss_kib=int(rss), threads=int(threads), descriptors=len(os.listdir(f"/proc/{process.pid}/fd"))
    )


def source_frames(progress):
    with progress.open("rb") as file:
        file.seek(max(0, progress.stat().st_size - 8192))
        return int(re.findall(rb"\bframe=(\d+)\b", file.read())[-1])


async def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    factory = provision(root / "factory", args.controller)
    username = pwd.getpwuid(os.getuid()).pw_name
    with ExitStack() as stack:
        network(stack, SELECTED)
        gateway, _, _, rtsp = start_media(stack, root, args)
        progress = root / "source-progress.txt"
        publisher = publish(
            stack, root, args, rtsp, gateway.publisher, "red", "640x360", SOURCE_FPS, progress=progress
        )
        config = root / "matter.json"
        private_file(
            config,
            json.dumps(
                dict(
                    version=1,
                    site_id="lab",
                    camera_id="cam",
                    lan_interface=SELECTED[1],
                    ice_address=SELECTED[2],
                    control=f"127.0.0.1:{gateway.control}",
                    rtsp=f"127.0.0.1:{rtsp}",
                    media_token_file=str(gateway.credentials["reader"]),
                    detection_token_file=str(gateway.credentials["independent"]),
                    factory_data=str(factory),
                    state_directory=str(root / "state"),
                    snapshot_ffmpeg=str(args.ffmpeg.resolve(strict=True)),
                )
            ),
        )

        def boot(label, *flags):
            return start(
                stack,
                root,
                label,
                [args.device, "--config", config, *flags],
                dict(os.environ, RUST_LOG="error", TMPDIR=str(root)),
            )

        def active(device):
            return wait_path(
                gateway, lambda p: p.get("ready") and len(p["readers"]) == 1, (device, *publisher)
            )

        device = boot("matter", "--initialize-state", "--open-commissioning-window")
        publication = (await asyncio.to_thread(active, device))["source"]["id"]
        command = [
            "sudo",
            "-n",
            "ip",
            "netns",
            "exec",
            SELECTED[0],
            "runuser",
            "-u",
            username,
            "--",
            "/usr/bin/env",
            "PATH=" + os.environ["PATH"],
            sys.executable,
            Path(__file__).resolve(),
            "--peer",
            "--runtime",
            root,
            "--controller",
            args.controller,
        ]
        with (root / "peer.log").open("wb") as log:
            peer = await asyncio.create_subprocess_exec(
                *map(str, command), stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log
            )

        async def response(timeout):
            line = await asyncio.wait_for(reader.readline(), timeout)
            assert line, "viewer supervisor exited"
            return json.loads(line)

        async def ask(name, timeout=35):
            writer.write((name + "\n").encode())
            await writer.drain()
            return await response(timeout)

        def write(kind, **fields):
            row = dict(kind=kind, elapsed_seconds=round(time.monotonic() - begun, 3), **fields)
            with (root / "samples.jsonl").open("a") as file:
                file.write(json.dumps(row) + "\n")
            return row

        begun = time.monotonic()
        writer = None
        try:
            async with asyncio.timeout(20):
                while True:
                    try:
                        reader, writer = await asyncio.open_unix_connection(root / "viewers.sock")
                        break
                    except (FileNotFoundError, ConnectionRefusedError):
                        assert peer.returncode is None, "viewer supervisor failed to start"
                        await asyncio.sleep(0.1)
            startup = await response(150)
            full = await ask("full")
            begun = time.monotonic()
            write("commissioned", **startup)
            write("saturation", **full)
            phases = iter([(0.25, "slow_offer"), (0.50, "restart"), (0.75, "revoke")])
            fraction, phase = next(phases)
            generation = 0
            limits = {}
            checks = []
            samples = 0
            previous = None
            while True:
                taps = await ask("sample")
                resource = resources(device)
                state = gateway.path()
                assert state["source"]["id"] == publication and len(state["readers"]) == 1
                frames = source_frames(progress)
                sample = write(
                    "sample", generation=generation, source_frames=frames, decoded=taps, **resource
                )
                samples += 1
                if generation not in limits:
                    limits[generation] = resource
                base = limits[generation]
                assert resource["rss_kib"] <= base["rss_kib"] + 32768
                assert resource["threads"] <= base["threads"] + 2
                assert resource["descriptors"] <= base["descriptors"] + 12
                if previous is not None:
                    seconds = sample["elapsed_seconds"] - previous["elapsed_seconds"]
                    if seconds >= 5:
                        source_fps = (frames - previous["source_frames"]) / seconds
                        decoded_fps = [
                            (new["frames"] - old["frames"]) / seconds
                            for new, old in zip(taps, previous["decoded"])
                        ]
                        assert 11 <= source_fps <= 19 and all(fps >= 10 for fps in decoded_fps)
                        write(
                            "rates",
                            source_fps=round(source_fps, 3),
                            decoded_fps=[round(fps, 3) for fps in decoded_fps],
                        )
                previous = sample
                now = time.monotonic()
                if phase and now >= begun + args.duration * fraction:
                    change = time.monotonic()
                    if phase == "restart":
                        await asyncio.to_thread(stop, device)
                        device = boot("matter-restarted")
                        assert (await asyncio.to_thread(active, device))["source"]["id"] == publication
                        generation += 1
                    result = await ask(phase, 115 if phase == "revoke" else 60)
                    checks.append(phase)
                    write(phase, recovery_seconds=round(time.monotonic() - change, 3), **result)
                    write("saturation", **await ask("full"))
                    fraction, phase = next(phases, (1, None))
                    previous = None
                    continue
                if now >= begun + args.duration:
                    break
                deadline = min(begun + args.duration * fraction, now + args.interval)
                await asyncio.sleep(max(0, deadline - time.monotonic()))
            assert (await ask("finish"))["closed"]
            assert await asyncio.wait_for(peer.wait(), 15) == 0
            return dict(
                duration_seconds=round(time.monotonic() - begun, 3),
                viewers=VIEWERS,
                samples=samples,
                checks=checks,
                media="generated H.264",
                rendered_fps=None,
            )
        finally:
            if writer is not None:
                writer.close()
            if peer.returncode is None:
                try:
                    await asyncio.wait_for(peer.wait(), 15)
                except TimeoutError:
                    peer.terminate()
                    await asyncio.wait_for(peer.wait(), 15)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--controller", type=Path, required=True)
    for name in ("device", "gateway", "publisher", "mediamtx", "ffmpeg"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--duration", type=int, default=86400)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--peer", action="store_true")
    args = parser.parse_args()
    if args.peer:
        asyncio.run(peer_main(args))
    else:
        if args.duration < 120 or args.interval < 5:
            parser.error("duration must be at least 120 seconds and interval at least 5 seconds")
        if any(
            getattr(args, key) is None for key in ("device", "gateway", "publisher", "mediamtx", "ffmpeg")
        ):
            parser.error("supply the pinned bridge and gateway fixture executables")
        result = asyncio.run(run(args))
        (args.runtime / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
