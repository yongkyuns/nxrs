#!/usr/bin/env python3
"""Observe Matter session admission, capacity and peer socket release."""

import argparse
import asyncio
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager, suppress
import errno
import json
import os
from pathlib import Path
import socket

from matter_audio_allocation import CLUSTER
from matter_commissioning_reference import provision
from matter_fabric_reference import Frames, attribute, controller
from matter_gateway_reference import start
from matter_reference import measured_peer
from matter_rtsp_reference import ports, publish, start_media, wait_path
from matter_source_access import private_file


def selected_port(peer, ip):
    transport = peer.getTransceivers()[0].receiver.transport.transport
    (pair,) = transport._connection._nominated.values()
    assert pair.remote_candidate.host == ip
    return pair.remote_candidate.port


def socket_owned(ip, port):
    with socket.socket(type=socket.SOCK_DGRAM) as probe:
        try:
            probe.bind((ip, port))
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            return True
        return False


async def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    with socket.socket(type=socket.SOCK_DGRAM) as probe:
        probe.bind((args.ip, 5540))
    factory = provision(root / "factory", args.controller)
    code = json.loads((factory.parent / "onboarding.json").read_bytes())["qrCode"]
    with ExitStack() as fixtures:
        gateway, _, _, rtsp = start_media(fixtures, root, args)
        settings = root / "matter.json"
        private_file(
            settings,
            json.dumps(
                dict(
                    version=1,
                    site_id="lab",
                    camera_id="cam",
                    control=f"127.0.0.1:{gateway.control}",
                    rtsp=f"127.0.0.1:{rtsp}",
                    ice_address=args.ip,
                    media_token_file=str(gateway.credentials["reader"]),
                    detection_token_file=str(gateway.credentials["independent"]),
                    factory_data=str(factory),
                    state_directory=str(root / "state"),
                )
            ),
        )
        device = start(
            fixtures,
            root,
            "matter",
            [args.device, "--config", settings, "--initialize-state", "--open-commissioning-window"],
            dict(os.environ, RUST_LOG="error", TMPDIR=str(root)),
        )
        media = publish(fixtures, root, args, rtsp, gateway.publisher, "red", "640x360", 15)
        wait_path(gateway, lambda p: p.get("ready") and len(p["readers"]) == 1, (device, *media))
        p1, p2 = ports(2)
        async with (
            controller(fixtures, root, args, "admin", p1, 32711) as admin,
            controller(
                fixtures,
                root,
                args,
                "guest",
                p2,
                32712,
            ) as guest,
        ):
            paired = await admin.request(
                "commission_with_code", code=code, network_only=True, discovery_timeout_s=60
            )
            camera = paired["node_id"]
            ticket = await admin.request(
                "open_commissioning_window", node_id=camera, timeout=180, iteration=1000, option=1
            )
            other = (
                await guest.request(
                    "commission_on_network",
                    setup_pin_code=ticket["setup_pin_code"],
                    ip_addr=args.ip,
                    discovery_timeout_s=60,
                )
            )["node_id"]
            guest_index = await attribute(guest, other, "0/62/5")

            async def sessions(c, node):
                path = "1/1363/0"
                result = await c.request(
                    "read_attribute", node_id=node, attribute_path=path, fabric_filtered=True
                )
                return {row["0"] for row in result[path]}

            async def allocations():
                return await attribute(admin, camera, f"1/{CLUSTER}/15")

            initial = await allocations()
            video = initial[0]["0"]
            original_refs = sum(row["12"] for row in initial)

            async def invoke(command, **payload):
                return await admin.request(
                    "device_command",
                    node_id=camera,
                    endpoint_id=1,
                    cluster_id=0x553,
                    command_name=command,
                    payload=payload,
                )

            async def solicit():
                return await invoke(
                    "SolicitOffer", streamUsage=3, originatingEndpointID=1, videoStreams=[video]
                )

            async def end(session_id):
                assert await invoke("EndSession", webRtcSessionId=session_id, reason=2) is None

            async def references(added):
                rows = await allocations()
                assert any(row["0"] == video for row in rows)
                actual = sum(row["12"] for row in rows)
                expected = original_refs + added
                assert actual == expected, (actual, expected)

            @asynccontextmanager
            async def play(c, node, flow):
                async with AsyncExitStack() as stack:
                    async with asyncio.timeout(25):
                        while True:
                            try:
                                playback = await stack.enter_async_context(
                                    measured_peer(c, node, 1, flow, 45, request_end=False)
                                )
                                break
                            except RuntimeError as exc:
                                if "(Matter status 203)" not in str(exc):
                                    raise
                                await asyncio.sleep(0.25)
                    yield playback

            async with play(guest, other, "camera-offer") as (g, gp, _), AsyncExitStack() as first:
                a, ap, _ = await first.enter_async_context(play(admin, camera, "receiver-offer"))
                [sid] = await sessions(admin, camera)
                admin_port, guest_port = selected_port(ap, args.ip), selected_port(gp, args.ip)
                assert socket_owned(args.ip, admin_port) and socket_owned(args.ip, guest_port)
                held = {(await solicit())["webRtcSessionId"] for _ in range(2)}
                assert len(held) == 2 and await sessions(admin, camera) == held | {sid}
                assert len(await sessions(guest, other)) == 1
                await references(4)
                try:
                    await solicit()
                except RuntimeError as exc:
                    if "(Matter status 137)" not in str(exc):
                        raise
                else:
                    raise AssertionError("a fifth offer was admitted")
                await references(4)

                await end(sid)
                assert not socket_owned(args.ip, admin_port), (
                    "EndSession replied with its UDP socket still bound"
                )
                await references(3)
                assert await sessions(admin, camera) == held
                await first.aclose()

                async with play(admin, camera, "camera-offer") as (again, peer, track):
                    [replacement] = (await sessions(admin, camera)) - held
                    assert replacement != sid
                    replacement_port = selected_port(peer, args.ip)
                    await references(4)
                    survivor = Frames()
                    drain = asyncio.create_task(survivor.consume(track))
                    try:
                        reply = await admin.request(
                            "device_command",
                            node_id=camera,
                            endpoint_id=0,
                            cluster_id=62,
                            command_name="RemoveFabric",
                            payload={"fabricIndex": guest_index},
                        )
                        assert reply["statusCode"] == 0
                        async with asyncio.timeout(10):
                            while socket_owned(args.ip, guest_port):
                                await asyncio.sleep(0.02)
                            previous = survivor.count
                            while survivor.count < previous + 20:
                                assert not drain.done()
                                await asyncio.sleep(0.02)
                    finally:
                        drain.cancel()
                        with suppress(asyncio.CancelledError):
                            await drain
                    assert socket_owned(args.ip, replacement_port)
                    await references(3)
                    assert (await attribute(admin, camera, "0/62/3")) == 1
                    await end(replacement)
                    assert not socket_owned(args.ip, replacement_port)
                for session_id in held:
                    await end(session_id)
                assert not await sessions(admin, camera)
                await references(0)
                return dict(
                    limit=4,
                    next_offer_status=137,
                    decoded_frames=[m["decoded_frames"] for m in (a, g, again)],
                    sockets_released_after_end_ack=2,
                    removed_fabric_socket_released=True,
                    survivor_frames_after_fabric_removal=20,
                    allocations_released=True,
                    sessions_remaining=0,
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
