#!/usr/bin/env python3
"""Commission a configured bridge and reassign media paths on its private gateway."""

import argparse
import asyncio
import base64
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket

from matter_commissioning_reference import provision
from matter_fabric_reference import attribute, controller
from matter_gateway_reference import DetectionPublisher, start, stop
from matter_reference import measured_peer
from matter_rtsp_reference import ports, publish, start_control, start_media
from matter_source_access import private_file, request


def describe(port, token, path, camera, *, username="reader"):
    # Exercise the same enrolled RTSP reader against both valid media paths.
    auth = base64.b64encode(f"{username}:{token}".encode()).decode()
    with socket.create_connection(("127.0.0.1", port), timeout=8) as conn:
        conn.sendall(
            (
                f"DESCRIBE rtsp://127.0.0.1:{port}/{path}?nxrs_source=lab.{camera} RTSP/1.0\r\n"
                f"CSeq: 1\r\nAccept: application/sdp\r\nAuthorization: Basic {auth}\r\n\r\n"
            ).encode()
        )
        with conn.makefile("rb") as reply:
            proto, code, _ = reply.readline(128).split(b" ", 2)
            assert proto == b"RTSP/1.0"
            return int(code)


async def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    with socket.socket(type=socket.SOCK_DGRAM) as probe:
        probe.bind((args.ip, 5540))
    factory = provision(root / "factory", args.controller)
    code = json.loads((factory.parent / "onboarding.json").read_bytes())["qrCode"]
    with ExitStack() as services:
        gateway, _, control, rtsp = start_media(services, root, args, paths=("cam", "other"))
        stop(control)
        config = json.loads(gateway.config.read_bytes())
        porch, study = "porch", "study"
        selected = config["cameras"][0]
        selected["camera_id"] = porch
        second = dict(
            selected, camera_id=study, stream_path="other", whep_endpoint="https://media.test/other/whep"
        )
        config["cameras"].append(second)
        config["viewers"][0]["camera_ids"] = [porch, study]
        config["viewers"][1]["camera_ids"] = [study]
        detector_token, other_token = secrets.token_hex(32), secrets.token_hex(32)

        def digest(s):
            return hashlib.sha256(s.encode()).hexdigest()

        config["viewers"].append(
            dict(
                viewer_id="detector",
                site_id="lab",
                scope="detections",
                camera_ids=[porch, study],
                token_sha256=digest(detector_token),
            )
        )
        config["publishers"][0]["camera_id"] = porch
        config["publishers"].append(
            dict(username="second", camera_id=study, token_sha256=digest(other_token))
        )
        gateway.config.write_text(json.dumps(config))
        control = start_control(services, root, args, gateway)
        source_token = gateway.credentials["reader"].read_text()
        sensor_token = root / "detection.token"
        private_file(sensor_token, detector_token)
        settings = dict(
            version=1,
            site_id="lab",
            camera_id=porch,
            control=f"127.0.0.1:{gateway.control}",
            rtsp=f"127.0.0.1:{rtsp}",
            media_token_file=str(gateway.credentials["reader"]),
            detection_token_file=str(sensor_token),
            factory_data=str(factory),
            ice_address=args.ip,
            state_directory=str(root / "state"),
        )
        if args.ffmpeg is not None:
            settings["snapshot_ffmpeg"] = str(args.ffmpeg.resolve(strict=True))
        deployment = root / "deployment.json"
        private_file(deployment, json.dumps(settings))

        def boot(label, *flags):
            return start(
                services,
                root,
                label,
                [args.device, "--config", deployment, *flags],
                dict(os.environ, RUST_LOG="error", TMPDIR=str(root)),
            )

        def mapping(camera, token=source_token):
            status, body = request(
                gateway.control, "GET", f"/service/v1/sites/lab/cameras/{camera}/source", token
            )
            return status, json.loads(body)

        assert mapping(porch)[1]["stream_path"] == "cam"
        assert mapping(porch, gateway.credentials["independent"].read_text())[0] == 404
        assert mapping(porch, detector_token)[0] == 403
        assert mapping(porch, gateway.publisher)[0] == 401
        deployment.write_text(json.dumps(dict(settings, site_id="other")))
        assert await asyncio.to_thread(boot("unknown-site", "--initialize-state").wait, 8) != 0
        assert not (root / "state").exists()
        deployment.write_text(json.dumps(settings))
        device = boot("first", "--initialize-state", "--open-commissioning-window")
        async with controller(services, root, args, "admin", ports(1)[0], 32710) as admin:
            result = await admin.request(
                "commission_with_code", code=code, network_only=True, discovery_timeout_s=60
            )
            node = result["node_id"]

            async def read(path):
                return await attribute(admin, node, path)

            async def ids():
                return (await read("1/57/18"), await read("3/57/18"))

            before = await ids()
            assert all(isinstance(s, str) and 1 <= len(s) <= 32 for s in before) and before[0] != before[1]
            assert await read("2/29/3") == [1, 3]
            assert await read("3/57/17") is False

            async def observe(publisher, session, level):
                async with asyncio.timeout(8):
                    while True:
                        assert publisher.publish(session, level) == 204
                        if await read("3/57/17") and (await read("3/1030/0")) == int(level):
                            return
                        await asyncio.sleep(0.15)

            async def play(expected, flow, after_decode=None):
                ended = asyncio.get_running_loop().create_future() if after_decode else None
                async with asyncio.timeout(30):
                    while True:
                        try:
                            async with measured_peer(
                                admin, node, 1, flow, 45, request_end=ended is None, end_reason=ended
                            ) as (media, peer, _):
                                assert media["dimensions"] == expected
                                # Inspect the actual remote ICE pair used by the receiver.
                                transport = peer.getTransceivers()[0].receiver.transport.transport
                                selected_pairs = list(transport._connection._nominated.values())
                                assert selected_pairs and all(
                                    p.remote_candidate.host == args.ip for p in selected_pairs
                                )
                                if after_decode:
                                    await after_decode(ended)
                                return media["decoded_frames"]
                        except RuntimeError as err:
                            if "(Matter status 203)" not in str(err):
                                raise
                            await asyncio.sleep(0.25)

            selected_feed = DetectionPublisher(gateway.internal, porch, "publisher", gateway.publisher)
            other_feed = DetectionPublisher(gateway.internal, study, "second", other_token)
            other_session = other_feed.register(control)
            # Keep the unselected source fresh across several gateway report periods.
            deadline = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < deadline:
                assert other_feed.publish(other_session, True) == 204
                assert await read("3/57/17") is False
                await asyncio.sleep(0.25)
            await observe(selected_feed, selected_feed.register(control), True)
            with ExitStack() as sources:
                publish(sources, root, args, rtsp, gateway.publisher, "red", "640x360", 15)
                publish(
                    sources,
                    root,
                    args,
                    rtsp,
                    other_token,
                    "blue",
                    "320x240",
                    10,
                    path="other",
                    username="second",
                )
                first_frames = await play([640, 360], "receiver-offer")
                assert describe(rtsp, source_token, "cam", porch) == 200
                assert describe(rtsp, source_token, "cam", study) == 401

            stop(control)
            selected["stream_path"], second["stream_path"] = "other", "cam"
            for camera in config["cameras"]:
                camera["whep_endpoint"] = "https://media.test/" + camera["stream_path"] + "/whep"
            config["cameras"].reverse()
            gateway.config.write_text(json.dumps(config))
            control = start_control(services, root, args, gateway)
            assert mapping(porch)[1]["stream_path"] == "other"
            with ExitStack() as sources:
                publish(sources, root, args, rtsp, gateway.publisher, "red", "320x240", 10, path="other")
                publish(sources, root, args, rtsp, other_token, "blue", "640x360", 15, username="second")
                replacement_frames = await play([320, 240], "camera-offer")
                assert describe(rtsp, source_token, "cam", study) == 200
                assert describe(rtsp, source_token, "cam", porch) == 401
                selected_session = selected_feed.register(control)
                await observe(selected_feed, selected_session, False)
                stop(device)
                for key in ("site_id", "camera_id"):
                    deployment.write_text(json.dumps(dict(settings, **{key: "different"})))
                    assert await asyncio.to_thread(boot("changed-" + key).wait, 8) != 0
                deployment.write_text(json.dumps(settings))
                device = boot("restored")
                async with asyncio.timeout(45):
                    while True:
                        assert device.poll() is None
                        try:
                            if await ids() == before:
                                break
                        except RuntimeError:
                            pass
                        await asyncio.sleep(0.25)
                assert await read("0/62/3") == 1 and await read("0/60/0") == 0

                async def revoke(ended):
                    await asyncio.to_thread(gateway.revoke)
                    assert await asyncio.wait_for(ended, 8) == 5
                    assert mapping(porch)[0] == 403
                    assert gateway.path("other")["ready"]
                    assert (
                        describe(
                            rtsp,
                            gateway.credentials["independent"].read_text(),
                            "cam",
                            study,
                            username="independent",
                        )
                        == 200
                    )
                    await observe(selected_feed, selected_session, True)

                restored_frames = await play([320, 240], "receiver-offer", after_decode=revoke)
            return dict(
                original_frames=first_frames,
                remapped_frames=replacement_frames,
                restarted_frames=restored_frames,
                frames_match_selected_dimensions=True,
                selected_ice_address_verified=True,
                retained_endpoint_ids=True,
                rejected_site_and_camera_changes=True,
                old_mapping_rtsp_status=401,
                selected_camera_occupancy=["unavailable", "person", "clear"],
                media_revocation_end_reason=5,
                publisher_and_independent_reader_survived=True,
                occupancy_observed_after_media_revocation=True,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "device", "controller", "gateway", "publisher", "mediamtx"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--ip", required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    (args.runtime / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
