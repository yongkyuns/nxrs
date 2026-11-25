#!/usr/bin/env python3
"""Verify a Matter offer follows its admitted camera publication through negotiation and playback."""

import argparse
import asyncio
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager
import json
import os
from pathlib import Path
import signal
import socket

from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpSender

from matter_attestation_reference import public_test_bundle
from matter_audio_allocation import CLUSTER
from matter_commissioning_reference import provision
from matter_fabric_reference import attribute, controller, packets
from matter_gateway_reference import start
from matter_reference import measured_peer, separate_candidates
from matter_rtsp_reference import ports, publish, start_media, start_media_server, wait_path
from matter_source_access import private_file


async def receiver_offer():
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    try:
        track = peer.addTransceiver("video", direction="recvonly")
        track.setCodecPreferences(
            [c for c in RTCRtpSender.getCapabilities("video").codecs if c.mimeType.lower() == "video/h264"]
        )
        await peer.setLocalDescription(await peer.createOffer())
        sdp, candidates = separate_candidates(peer.localDescription.sdp)
        assert candidates
        return sdp
    finally:
        await peer.close()


async def offline(admin, node, video, sdp):
    # Real provider commands distinguish the device response from local controller policy.
    for name, fields in (
        ("SolicitOffer", {}),
        ("ProvideOffer", {"webRtcSessionId": None, "sdp": sdp}),
    ):
        try:
            await admin.request(
                "device_command",
                node_id=node,
                endpoint_id=1,
                cluster_id=0x553,
                command_name=name,
                payload=dict(streamUsage=3, originatingEndpointID=1, videoStreams=[video], **fields),
            )
        except RuntimeError as error:
            if "(Matter status 203)" not in str(error):
                raise
        else:
            raise AssertionError(f"{name} admitted an offline source")


async def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    factory = provision(root / "factory", args.controller)
    code = json.loads((factory.parent / "onboarding.json").read_bytes())["qrCode"]
    with socket.socket(type=socket.SOCK_DGRAM) as probe:
        probe.bind((args.ip, 5540))

    with ExitStack() as services:
        gateway, server, control, rtsp = start_media(services, root, args)
        config = root / "matter.json"
        settings = dict(
            version=1,
            site_id="lab",
            camera_id="cam",
            lan_interface=args.interface,
            ice_address=args.ip,
            control=f"127.0.0.1:{gateway.control}",
            rtsp=f"127.0.0.1:{rtsp}",
            media_token_file=str(gateway.credentials["reader"]),
            detection_token_file=str(gateway.credentials["independent"]),
            factory_data=str(factory),
            state_directory=str(root / "state"),
            snapshot_ffmpeg=str(args.ffmpeg.resolve(strict=True)),
        )
        attestation_flags = []
        if args.sdk is not None:
            bundle = root / "attestation"
            public_test_bundle(args.sdk, bundle)
            settings.update(attestation_directory=str(bundle), host_power_source="dc")
            attestation_flags = ["--allow-test-attestation"]
        private_file(config, json.dumps(settings))
        device = start(
            services,
            root,
            "matter",
            [args.device, "--config", config, "--initialize-state", "--open-commissioning-window",
             *attestation_flags],
            dict(os.environ, RUST_LOG="error", TMPDIR=str(root)),
        )

        def source(stack, color, size, rate):
            children = publish(stack, root, args, rtsp, gateway.publisher, color, size, rate)
            path = wait_path(
                gateway, lambda p: p.get("ready") and len(p["readers"]) == 1, (*children, device, server)
            )
            return path["source"]["id"]

        async with controller(services, root, args, "admin", ports(1)[0], 32709) as admin:
            paired = await admin.request(
                "commission_with_code", code=code, network_only=True, discovery_timeout_s=60
            )
            node = paired["node_id"]
            sessions = "1/1363/0"
            video = (await attribute(admin, node, f"1/{CLUSTER}/15"))[0]["0"]
            identity = await attribute(admin, node, "1/57/18")
            assert isinstance(identity, str) and identity
            sdp = await receiver_offer()
            await offline(admin, node, video, sdp)

            async def held_event(event_type, keys):
                async with asyncio.timeout(12):
                    while True:
                        item = await admin.events.get()
                        data = item["data"]
                        if (
                            data.get("client_session_key") in keys
                            and data.get("node_id") == node
                            and data.get("endpoint_id") == 1
                            and (
                                data.get("event_type") == event_type
                                or (event_type == "end" and item["event"] == "camera_session_ended")
                            )
                        ):
                            return data

            async def released():
                async with asyncio.timeout(8):
                    while await attribute(admin, node, sessions):
                        await asyncio.sleep(0.1)

            @asynccontextmanager
            async def playback(flow, **options):
                # MediaMTX registers its reader before the first authorized keyframe.
                async with AsyncExitStack() as stack:
                    async with asyncio.timeout(20):
                        while True:
                            try:
                                peer = await stack.enter_async_context(
                                    measured_peer(admin, node, 1, flow, 45, **options)
                                )
                                break
                            except RuntimeError as error:
                                if "(Matter status 203)" not in str(error):
                                    raise
                                await asyncio.sleep(0.25)
                    yield peer

            with ExitStack() as first:
                original = source(first, "red", "640x360", 15)
                async with playback("receiver-offer") as (before, _, _):
                    assert before["dimensions"] == [640, 360]

                async def hold(key, event, **fields):
                    await admin.request(
                        "camera_start_stream",
                        node_id=node,
                        endpoint_id=1,
                        stream_usage="LiveView",
                        audio=False,
                        allow_eviction=False,
                        client_session_key=key,
                        **fields,
                    )
                    return (await held_event(event, {key}))["webrtc_session_id"]

                # One controller does not answer the camera offer. With the other
                # session it receives the camera's answer and never trickles ICE.
                waiting = {
                    "unanswered": await hold("unanswered", "offer"),
                    "no-ice": await hold("no-ice", "answer", sdp=sdp),
                }
                rows = await attribute(admin, node, sessions)
                assert len(rows) == 2 and {row["0"] for row in rows} == set(waiting.values())

            with ExitStack() as replacement:
                replacement_id = source(replacement, "blue", "320x240", 10)
                assert replacement_id != original
                ended_reasons = {}
                while waiting:
                    try:
                        ended = await held_event("end", waiting)
                    except TimeoutError:
                        rows = await attribute(admin, node, sessions)
                        held = [key for key, sid in waiting.items() if any(r["0"] == sid for r in rows)]
                        raise AssertionError(
                            f"Missing EndSession after replacement; still listed: {held}"
                        ) from None
                    key = ended["client_session_key"]
                    assert ended["webrtc_session_id"] == waiting.pop(key)
                    ended_reasons[key] = ended.get("reason", ended.get("data", {}).get("reason"))
                assert ended_reasons == {"unanswered": 5, "no-ice": 5}
                await released()

                active_end = asyncio.get_running_loop().create_future()
                async with playback("camera-offer", request_end=False, end_reason=active_end) as (
                    after,
                    retired_peer,
                    _,
                ):
                    assert after["dimensions"] == [320, 240]
                    lost_at = asyncio.get_running_loop().time()
                    server.kill()
                    assert await asyncio.to_thread(server.wait, 5) == -signal.SIGKILL
                    assert await asyncio.wait_for(active_end, 12) == 5
                    await released()
                    packets_after_loss = await packets(retired_peer)
                    assert packets_after_loss > 0
                    await offline(admin, node, video, sdp)
                    assert await packets(retired_peer) == packets_after_loss
                    assert device.poll() is None and control.poll() is None

            await released()
            server = await asyncio.to_thread(start_media_server, services, root, args, rtsp)
            await asyncio.to_thread(gateway.ready, control)
            # Starting MediaMTX alone is not sufficient to admit another peer.
            await offline(admin, node, video, sdp)
            with ExitStack() as recovered:
                recovery_id = source(recovered, "green", "640x360", 15)
                assert recovery_id not in (original, replacement_id)
                async with playback("receiver-offer") as (restored, _, _):
                    assert restored["dimensions"] == [640, 360]
                recovery_seconds = round(asyncio.get_running_loop().time() - lost_at, 3)
                assert await attribute(admin, node, "1/57/18") == identity
                assert await attribute(admin, node, "0/62/3") == 1
            assert device.poll() is None and control.poll() is None
            await released()
            assert await attribute(admin, node, sessions) == []
            return dict(
                original_frames=before["decoded_frames"],
                original_dimensions=before["dimensions"],
                unanswered_end_reason=ended_reasons["unanswered"],
                no_ice_end_reason=ended_reasons["no-ice"],
                replacement_frames=after["decoded_frames"],
                replacement_dimensions=after["dimensions"],
                active_end_reason=active_end.result(),
                offline_solicit_status=203,
                offline_provide_status=203,
                media_server_restarted_after_sigkill=True,
                media_server_recovery_seconds=recovery_seconds,
                restored_frames=restored["decoded_frames"],
                retained_camera_identity=True,
                final_sessions=0,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "controller", "device", "gateway", "publisher", "mediamtx", "ffmpeg"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--sdk", type=Path, help="pinned SDK checkout for the packaged bridge")
    parser.add_argument("--interface", required=True)
    parser.add_argument("--ip", required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    (args.runtime / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
