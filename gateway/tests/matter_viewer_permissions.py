#!/usr/bin/env python3
"""Verify per-node access to one camera on one commissioned Matter fabric."""

import argparse
import asyncio
from contextlib import ExitStack, asynccontextmanager
import json
from pathlib import Path
import socket

from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpSender, RTCSessionDescription

from matter_audio_reference import publish_av
from matter_commissioning_reference import provision
from matter_fabric_reference import Frames, attribute, controller, packets
from matter_reference import measured_peer
from matter_rtsp_reference import ports, start_device, start_media, wait_path


@asynccontextmanager
async def viewer(root, args, port, *, existing_storage=None):
    if existing_storage is not None:
        assert existing_storage.is_dir(), "missing enrolled viewer"
    install = args.controller.absolute().parent.parent.parent
    with (root / "viewer.log").open("xb") as log:
        child = await asyncio.create_subprocess_exec(
            "node",
            Path(__file__).with_name("matter") / "viewer.cjs",
            install,
            existing_storage or root / "viewer-state",
            args.interface,
            str(port),
            stderr=log,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )

    async def exchange(op=None, **kwargs):
        if op is not None:
            child.stdin.write(json.dumps(dict(op=op, **kwargs)).encode() + b"\n")
            await child.stdin.drain()
        line = await asyncio.wait_for(child.stdout.readline(), 35)
        assert line and line.startswith(b"{"), "viewer RPC failed"
        return json.loads(line)

    try:
        yield exchange
    finally:
        child.stdin.close()
        try:
            await asyncio.wait_for(child.wait(), 5)
        except TimeoutError:
            child.terminate()
            await child.wait()


@asynccontextmanager
async def second_stream(call, camera, video, *, camera_ends=False):
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    try:
        video_transceiver = peer.addTransceiver("video", direction="recvonly")
        video_transceiver.setCodecPreferences(
            [
                codec
                for codec in RTCRtpSender.getCapabilities("video").codecs
                if codec.mimeType.lower() == "video/h264"
            ]
        )
        await peer.setLocalDescription(await peer.createOffer())
        remote = await call("offer", camera=camera, sdp=peer.localDescription.sdp, video=video)
        await peer.setRemoteDescription(RTCSessionDescription(sdp=remote["sdp"], type="answer"))
        yield peer, video_transceiver.receiver.track, remote["id"]
        if not camera_ends:
            assert await call("end", camera=camera, id=remote["id"]) == {"ended": True}
    finally:
        await peer.close()


async def frames(track, count):
    async with asyncio.timeout(10):
        decoded = [await track.recv() for _ in range(count)]
    assert all((frame.width, frame.height) == (320, 240) for frame in decoded)
    assert all(a.pts < b.pts for a, b in zip(decoded, decoded[1:]))
    return len(decoded)


async def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    factory = provision(root / "factory", args.controller)
    code = json.loads((factory.parent / "onboarding.json").read_bytes())["qrCode"]
    with socket.socket(type=socket.SOCK_DGRAM) as check:
        check.bind((args.ip, 5540))
    with ExitStack() as stack:
        gateway, _, _, rtsp = start_media(stack, root, args)
        publishers = publish_av(stack, root, args, rtsp, gateway.publisher)
        device = start_device(
            stack,
            root,
            args,
            gateway,
            rtsp,
            root / "camera",
            initialize=True,
            extra_args=["--factory-data", factory, "--open-commissioning-window"],
        )
        wait_path(gateway, lambda p: p.get("ready") and len(p["readers"]) == 1, (*publishers, device))
        port, ws_port = ports(2)
        fabric = 32707
        async with controller(stack, root, args, "administrator", ws_port, fabric) as admin:

            async def commission(ticket):
                node = await admin.request(
                    "commission_with_code", code=ticket, network_only=True, discovery_timeout_s=60
                )
                return node["node_id"]

            camera = await commission(code)
            async with viewer(root, args, port) as call:
                info = await call()
                other = await commission(info["code"])
                identity = await call("identity")
                assert int(identity["node"]) == other and int(identity["fabric"]) == fabric
                assert camera != other and await attribute(admin, camera, "0/62/3") == 1
                acl = await attribute(admin, camera, "0/31/0")
                assert len(acl) == 1 and acl[0]["1"] == 5
                assert other not in [int(subject) for subject in acl[0]["3"]]
                grant = {
                    "1": 4,
                    "2": 2,
                    "3": [other],
                    "4": [{"0": 0x553, "1": 1, "2": None}],
                    "254": acl[0]["254"],
                }
                viewer_acl = await attribute(admin, other, "0/31/0")
                camera_signaling = {
                    "1": 3,
                    "2": 2,
                    "3": [camera],
                    "4": [{"0": 0x554, "1": info["endpoint"], "2": None}],
                    "254": viewer_acl[0]["254"],
                }
                await admin.request(
                    "write_attribute",
                    node_id=other,
                    attribute_path="0/31/0",
                    value=[*viewer_acl, camera_signaling],
                )
                assert await attribute(admin, other, "0/31/0") == [*viewer_acl, camera_signaling]

                async def acl_write(entries):
                    await admin.request(
                        "write_attribute", node_id=camera, attribute_path="0/31/0", value=entries
                    )
                    applied = await attribute(admin, camera, "0/31/0")
                    assert applied == entries, "camera did not apply the requested ACL"

                async def expect(expected):
                    observed = await call("sessions", camera=camera)
                    assert observed == expected, f"CurrentSessions read returned {observed}"

                async def ended(session_id):
                    async with asyncio.timeout(10):
                        while await call("lastEnd", camera=camera) != {"id": session_id, "reason": 5}:
                            await asyncio.sleep(0.05)

                async with measured_peer(admin, camera, 1, "receiver-offer", 45) as (_, _, track):
                    await expect({"status": 0x7E})
                    (original,) = await attribute(admin, camera, "1/1363/0")
                    await acl_write([*acl, grant])
                    await expect({"sessions": 1})
                    async with second_stream(call, camera, original["4"], camera_ends=True) as (
                        peer,
                        second,
                        sid,
                    ):
                        counts = await asyncio.gather(frames(track, 20), frames(second, 20))
                        sessions = await attribute(admin, camera, "1/1363/0")
                        assert len(sessions) == 2
                        assert {int(s["1"]) for s in sessions} == {int(acl[0]["3"][0]), other}
                        assert {s["254"] for s in sessions} == {acl[0]["254"]}
                        before = gateway.path()
                        await acl_write(acl)
                        await expect({"status": 0x7E})
                        # Immediate reinstatement must still require a fresh negotiation.
                        await acl_write([*acl, grant])
                        await ended(sid)
                        await expect({"sessions": 1})
                        active, retired = Frames((320, 240)), Frames((320, 240))
                        tasks = [
                            asyncio.create_task(active.consume(track)),
                            asyncio.create_task(retired.consume(second)),
                        ]
                        try:
                            await asyncio.sleep(1)
                            packet_base = await packets(peer)
                            a, b = active.count, retired.count
                            await asyncio.sleep(4)
                            remaining = active.count - a
                            assert (
                                remaining >= 30 and retired.count == b and await packets(peer) == packet_base
                            )
                            current = await attribute(admin, camera, "1/1363/0")
                            assert current == [original]
                            after = gateway.path()
                            assert after["source"]["id"] == before["source"]["id"]
                            assert len(after["readers"]) == 1 and before["readers"] == after["readers"]
                        finally:
                            for task in tasks:
                                task.cancel()
                            for result in await asyncio.gather(*tasks, return_exceptions=True):
                                if isinstance(result, Exception):
                                    raise result
                    pending = await call("hold", camera=camera, video=original["4"])
                    await expect({"sessions": 2})
                    await acl_write(acl)
                    await ended(pending["id"])
                    assert await attribute(admin, camera, "1/1363/0") == [original]
                    await acl_write([*acl, grant])
                    async with second_stream(call, camera, original["4"]) as (_, fresh, new_id):
                        assert new_id not in (sid, pending["id"])
                        reopened = await frames(fresh, 15)
                assert await attribute(admin, camera, "0/62/3") == 1
                return dict(
                    camera_fabrics=1,
                    distinct_controller_nodes=2,
                    viewer_read_restored=True,
                    viewer_revoked_status=0x7E,
                    simultaneous_decoded_frames=counts,
                    revoked_end_reason=5,
                    revoked_observation_seconds=4,
                    revoked_rtp_packets=0,
                    revoked_decoded_frames=0,
                    continuing_frames=remaining,
                    new_session_decoded_frames=reopened,
                    same_upstream_reader=True,
                    pending_revoked_end_reason=5,
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
