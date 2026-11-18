#!/usr/bin/env python3
"""Exercise JPEG capture across two Matter fabrics while the live peer plays video."""

import argparse
import asyncio
import base64
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager, suppress
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import sys

import av

from matter_commissioning_reference import provision
from matter_fabric_reference import Frames, attribute, controller
from matter_gateway_reference import start
from matter_reference import measured_peer
from matter_rtsp_reference import ports, publish, start_media, wait_path
from matter_source_access import private_file


CLUSTER = 0x551
CAPABILITIES = f"1/{CLUSTER}/10"
ALLOCATIONS = f"1/{CLUSTER}/17"
LIVE_PRIVACY = f"1/{CLUSTER}/20"


def verify_jpeg(result, size, color):
    assert result["codec"] == "JPEG"
    assert result["resolution"] == dict(zip(("width", "height"), size))
    data = base64.b64decode(result["data"], validate=True)
    # A real JPEG above Matter UDP's payload budget exercises the controller's TCP path.
    assert 1600 < len(data) <= 48 * 1024
    with av.open(BytesIO(data), format="mjpeg") as decoded:
        (frame,) = decoded.decode(video=0)
        assert (frame.width, frame.height) == size
        rgb = bytes(frame.reformat(format="rgb24").planes[0])
        assert rgb[color] > 175 and rgb[2 - color] < 60
    return len(data)


def encoder_probe(root, ffmpeg):
    path = root / "encoder-probe"
    delay = root / "delay-encoder"
    pid_file = root / "encoder.pid"
    resume = root / "resume-encoder"
    program = f"""#!{sys.executable}
import os
from pathlib import Path
import sys
import time

delay = Path({str(delay)!r})
if delay.is_file():
    hold = delay.read_text() == "hold"
    delay.unlink()
    Path({str(pid_file)!r}).write_text(str(os.getpid()))
    if hold:
        resume = Path({str(resume)!r})
        deadline = time.monotonic() + 30
        while not resume.exists():
            if time.monotonic() >= deadline:
                raise SystemExit(1)
            time.sleep(0.02)
        resume.unlink()
    else:
        time.sleep(30)
os.execv({str(ffmpeg)!r}, [{str(ffmpeg)!r}, *sys.argv[1:]])
"""
    private_file(path, program)
    path.chmod(0o700)
    return path, delay, pid_file


async def wait_for_encoder(pid_file, capture):
    async with asyncio.timeout(4):
        while not pid_file.is_file():
            if capture.done():
                capture.result()
                raise AssertionError("capture finished before the delayed encoder started")
            await asyncio.sleep(0.02)
    pid = int(pid_file.read_text())
    os.kill(pid, 0)
    return pid


async def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    with socket.socket(type=socket.SOCK_DGRAM) as probe:
        probe.bind((args.ip, 5540))
    factory = provision(root / "factory", args.controller)
    code = json.loads((factory.parent / "onboarding.json").read_bytes())["qrCode"]

    with ExitStack() as fixtures:
        gateway, _, _, rtsp = start_media(fixtures, root, args)
        ffmpeg = args.ffmpeg.resolve(strict=True)
        encoder, delay_encoder, pid_file = encoder_probe(root, ffmpeg)
        config = root / "matter.json"
        private_file(
            config,
            json.dumps(
                dict(
                    version=1,
                    site_id="lab",
                    camera_id="cam",
                    ice_address=args.ip,
                    control=f"127.0.0.1:{gateway.control}",
                    rtsp=f"127.0.0.1:{rtsp}",
                    media_token_file=str(gateway.credentials["reader"]),
                    detection_token_file=str(gateway.credentials["independent"]),
                    factory_data=str(factory),
                    state_directory=str(root / "state"),
                    snapshot_ffmpeg=str(encoder),
                )
            ),
        )
        device = start(
            fixtures,
            root,
            "matter",
            [
                args.device,
                "--config",
                config,
                "--initialize-state",
                "--open-commissioning-window",
            ],
            dict(os.environ, RUST_LOG="error", TMPDIR=str(root)),
        )
        admin_port, viewer_port = ports(2)
        async with (
            controller(fixtures, root, args, "admin", admin_port, 32713) as client,
            controller(fixtures, root, args, "viewer", viewer_port, 32714) as viewer,
        ):
            pairing = await client.request(
                "commission_with_code", code=code, network_only=True, discovery_timeout_s=60
            )
            node = pairing["node_id"]
            target = dict(node_id=node, endpoint_id=1)
            invitation = await client.request(
                "open_commissioning_window", node_id=node, timeout=180, iteration=1000, option=1
            )
            paired_viewer = await viewer.request(
                "commission_on_network",
                setup_pin_code=invitation["setup_pin_code"],
                ip_addr=args.ip,
                discovery_timeout_s=60,
            )
            viewer_node = paired_viewer["node_id"]
            assert await attribute(client, node, "0/62/5") != await attribute(viewer, viewer_node, "0/62/5")
            caps = await attribute(client, node, CAPABILITIES)
            assert caps == [{"0": {"0": 640, "1": 360}, "1": 1, "2": 0, "3": False, "4": False}]
            assert await attribute(client, node, ALLOCATIONS) == []

            async def capture(**params):
                return await client.request("camera_snapshot", **target, codec="JPEG", **params)

            async def rejected_capture(stream, status=203, *, actor=client, capture_node=node):
                try:
                    await actor.request(
                        "device_command",
                        node_id=capture_node,
                        endpoint_id=1,
                        cluster_id=CLUSTER,
                        command_name="CaptureSnapshot",
                        payload={
                            "snapshotStreamId": stream,
                            "requestedResolution": {"width": 640, "height": 360},
                        },
                    )
                except RuntimeError as error:
                    if f"(Matter status {status})" not in str(error):
                        raise
                else:
                    raise AssertionError("snapshot capture succeeded while unavailable")

            async def privacy(enabled):
                reply = await client.request(
                    "write_attribute", node_id=node, attribute_path=LIVE_PRIVACY, value=enabled
                )
                assert reply[0]["Status"] == 0 and await attribute(client, node, LIVE_PRIVACY) is enabled

            @asynccontextmanager
            async def play(flow, frame_count):
                async with AsyncExitStack() as stack:
                    async with asyncio.timeout(30):
                        while True:
                            try:
                                peer = await stack.enter_async_context(
                                    measured_peer(client, node, 1, flow, frame_count)
                                )
                                break
                            except RuntimeError as error:
                                if "(Matter status 203)" not in str(error):
                                    raise
                                await asyncio.sleep(0.25)
                    yield peer

            with ExitStack() as red:
                children = publish(red, root, args, rtsp, gateway.publisher, "red", "640x360", 15)
                wait_path(gateway, lambda p: p.get("ready") and len(p["readers"]) == 1, (device, *children))
                async with play("receiver-offer", 45) as (live, _, track):
                    reader = gateway.path()["readers"][0]["id"]
                    frames = Frames()
                    consumer = asyncio.create_task(frames.consume(track))
                    delay_encoder.write_text("hold")
                    in_flight = asyncio.create_task(capture())
                    try:
                        pid = await wait_for_encoder(pid_file, in_flight)
                        (allocation,) = await attribute(viewer, viewer_node, ALLOCATIONS)
                        await rejected_capture(allocation["0"], 156, actor=viewer, capture_node=viewer_node)
                        assert not in_flight.done()
                        before = frames.count
                        async with asyncio.timeout(4):
                            while frames.count < before + 15:
                                assert not consumer.done()
                                await asyncio.sleep(0.03)
                        concurrent_frames = frames.count - before
                        os.kill(pid, 0)
                        (root / "resume-encoder").touch()
                        first = await asyncio.wait_for(in_flight, 5)
                        next_capture = asyncio.get_running_loop().time() + 1.05
                        assert first["stream_id"] == allocation["0"]
                        await rejected_capture(first["stream_id"], status=156)
                        first_bytes = verify_jpeg(first, (640, 360), 0)
                        before = frames.count
                        async with asyncio.timeout(4):
                            while frames.count < before + 15:
                                assert not consumer.done()
                                await asyncio.sleep(0.03)
                        assert gateway.path()["readers"][0]["id"] == reader
                    finally:
                        consumer.cancel()
                        in_flight.cancel()
                        with suppress(asyncio.CancelledError):
                            await consumer
                        await asyncio.gather(in_flight, return_exceptions=True)
                stream = first["stream_id"]
                (allocated,) = await attribute(client, node, ALLOCATIONS)
                assert allocated["0"] == stream
                await privacy(True)
                await rejected_capture(stream)
                await privacy(False)
                await asyncio.sleep(max(0, next_capture - asyncio.get_running_loop().time()))
                pid_file.unlink()
                delay_encoder.touch()
                in_flight = asyncio.create_task(rejected_capture(stream))
                try:
                    pid = await wait_for_encoder(pid_file, in_flight)
                    await asyncio.to_thread(red.close)
                    await asyncio.wait_for(in_flight, 5)
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        pass
                    else:
                        raise AssertionError("encoder remained after source loss rejected the snapshot")
                finally:
                    in_flight.cancel()
                    with suppress(asyncio.CancelledError):
                        await in_flight
            wait_path(gateway, lambda p: not p.get("ready"), (device,))
            await rejected_capture(stream)

            with ExitStack() as blue:
                children = publish(blue, root, args, rtsp, gateway.publisher, "blue", "640x360", 15)
                wait_path(gateway, lambda p: p.get("ready") and len(p["readers"]) == 1, (device, *children))
                # Demand a decoded first keyframe before making the second capture.
                async with play("camera-offer", 30):
                    second = await viewer.request(
                        "camera_snapshot", node_id=viewer_node, endpoint_id=1, codec="JPEG",
                        resolution={"width": 320, "height": 180},
                    )
                    second_bytes = verify_jpeg(second, (320, 180), 2)
                assert second["stream_id"] == stream
                await client.request("camera_release_stream", **target, kind="snapshot", stream_id=stream)
                assert await attribute(client, node, ALLOCATIONS) == []
            return dict(
                jpeg_bytes=[first_bytes, second_bytes],
                dimensions=[[640, 360], [320, 180]],
                distinct_publications=True,
                live_frames_before_capture=live["decoded_frames"],
                additional_frames_after_capture=15,
                shared_reader=True,
                private_and_offline_rejected=True,
                rate_limit_busy_status=156,
                concurrent_fabrics=2,
                concurrent_busy_status=156,
                live_frames_while_encoder_held=concurrent_frames,
                source_loss_cancelled_and_reaped_encoder=True,
                allocation_reused_and_released=True,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "device", "controller", "gateway", "publisher", "mediamtx"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--ip", required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    (args.runtime / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
