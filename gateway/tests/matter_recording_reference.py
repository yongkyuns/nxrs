#!/usr/bin/env python3
"""Verify authorized Matter recording privacy against a real guarded recorder."""
import argparse
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import shutil
import subprocess
import time
from urllib.request import Request, urlopen

import av

from matter_attestation_reference import public_test_bundle
from matter_commissioning_reference import provision
from matter_fabric_reference import attribute, controller
from matter_gateway_reference import ROOT, DetectionPublisher, start, stop
from matter_rtsp_reference import ports, publish, start_control, start_media_server, wait_path
from matter_source_access import StrictMedia, private_file, request

RECORDING_PRIVACY = "1/1361/19"
CONTROL_PATH = "/internal/cameras/cam/recording"


async def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    rtsp, api, control, internal = ports(4)
    gateway = StrictMedia(root, api, control, internal)
    enrollment = root / "enrollment"
    subprocess.run(["python3", ROOT / "gateway/tools/stage_matter.py", gateway.config, enrollment,
                    "--camera", "cam", "--identity", "matter", "--recording"], check=True)
    gateway.config = enrollment / "control.json"
    recorder_token = (enrollment / "recording.token").read_text().strip()
    factory = provision(root / "factory", args.controller)
    attestation = root / "attestation"
    public_test_bundle(args.sdk, attestation)
    deployment = root / "bridge.json"
    private_file(deployment, json.dumps(dict(version=1, site_id="lab", camera_id="cam",
        control=f"127.0.0.1:{control}", rtsp=f"127.0.0.1:{rtsp}", factory_data=str(factory),
        state_directory=str(root/"state"), media_token_file=str(enrollment/"media.token"),
        attestation_directory=str(attestation), snapshot_ffmpeg=shutil.which("ffmpeg"), host_power_source="dc",
        detection_token_file=str(enrollment/"detections.token"), ice_address=args.ip,
        lan_interface=args.interface, recording_control=dict(address=f"127.0.0.1:{internal}",
                                                              token_file=str(enrollment/"recording.token")))))
    private_file(root/"mediamtx.json", json.dumps(dict(logLevel="warn",
        rtspAddress=f"127.0.0.1:{rtsp}", rtspTransports=["tcp"], rtspAuthMethods=["basic"],
        api=True, apiAddress=f"127.0.0.1:{api}", rtmp=False, hls=False, srt=False, moq=False, webrtc=False,
        authMethod="http", authHTTPExclude=[], authHTTPAddress=f"http://127.0.0.1:{internal}/internal/mediamtx/auth",
        paths={"cam": dict(overridePublisher=False, record=True, recordRequiresPermit=True,
            recordPath=str(root/"archive/%path/%Y-%m-%d_%H-%M-%S-%f"),
            recordPartDuration="250ms", recordSegmentDuration="2s")})))

    def archive():
        return {path: path.stat().st_size for path in (root/"archive").rglob("*.mp4")}

    def recording(path, enabled):
        return (path.get("ready") and path.get("recordingEnabled") is enabled
                and path.get("recorderPresent") is enabled)

    detections = DetectionPublisher(internal, "cam", "publisher", gateway.publisher)
    detection_token = (enrollment / "detections.token").read_text().strip()

    def snapshot():
        req = Request(f"http://127.0.0.1:{control}/v1/cameras/cam/detections",
                      headers={"Authorization": "Bearer " + detection_token})
        with urlopen(req, timeout=2) as stream:
            for line in stream:
                if line.startswith(b"data:"):
                    return json.loads(line[5:])
        raise AssertionError("missing detection snapshot")

    with ExitStack() as stack:
        server = start_media_server(stack, root, args, rtsp)
        gateway_process = start_control(stack, root, args, gateway)
        children = publish(stack, root, args, rtsp, gateway.publisher, "red", "320x240", 15)
        initial = wait_path(gateway, lambda p: recording(p, False), children)
        assert not archive()
        assert request(internal, "GET", CONTROL_PATH)[0] == 401
        assert request(internal, "GET", CONTROL_PATH, gateway.credentials["reader"].read_text())[0] == 403
        assert request(internal, "GET", CONTROL_PATH.replace("/cam/", "/other/"), recorder_token)[0] == 403
        status, body = request(internal, "GET", CONTROL_PATH, recorder_token)
        assert status == 200 and json.loads(body) == {"enabled": False}

        def boot(label, initialize=False):
            options = ["--initialize-state", "--open-commissioning-window"] if initialize else []
            return start(stack, root, label, [args.device, "--config", deployment, "--allow-test-attestation", *options])

        device = boot("first", True)
        wait_path(gateway, lambda p: p.get("ready") and len(p["readers"]) == 1, (*children, device))
        async with controller(stack, root, args, "admin", ports(1)[0], 32704) as admin:
            code = json.loads((factory.parent/"onboarding.json").read_bytes())["qrCode"]
            pair = await admin.request("commission_with_code", code=code, network_only=True, discovery_timeout_s=60)
            node = pair["node_id"]

            async def read(expected):
                async with asyncio.timeout(20):
                    while True:
                        try:
                            if await attribute(admin, node, RECORDING_PRIVACY) is expected:
                                return
                        except RuntimeError:
                            pass
                        assert device.poll() is None, "bridge stopped"
                        await asyncio.sleep(.2)

            async def write(value):
                statuses = await admin.request("write_attribute", node_id=node, attribute_path=RECORDING_PRIVACY,
                                               value=value)
                assert [status["Status"] for status in statuses] == [0]
                assert recording(gateway.path(), not value), "Matter replied before the native transition"
                await read(value)

            async def reachability(expected):
                async with asyncio.timeout(5):
                    while await attribute(admin, node, "3/57/17") is not expected:
                        await asyncio.sleep(.1)

            async def publish_fresh_detection():
                source = detections.register(gateway_process)
                # The first post-registration frame can still carry a cached box.
                assert detections.publish(source, True) == 204
                fence = snapshot()
                assert fence["presence"] == "unavailable" and fence["source"] is None
                assert detections.publish(source, True) == 204
                assert snapshot()["presence"] == "person"
                await reachability(True)
                assert await attribute(admin, node, "3/1030/0") == 1
                return source

            await read(True)
            assert not archive()
            assert detections.post("detection-sessions", {})[0] == 503
            await write(False)
            await asyncio.sleep(3.5)
            assert recording(gateway.path(), True) and archive()
            active = gateway.path()
            device.kill()
            device.wait(timeout=5)
            wait_path(gateway, lambda p: recording(p, False), children)
            device = boot("allow-restart")
            await read(False)
            wait_path(gateway, lambda p: recording(p, True), (*children, device))
            source = await publish_fresh_detection()
            start_stop = time.monotonic()
            if args.minimum_stop_ms:
                stopping = asyncio.create_task(write(True))
                try:
                    async with asyncio.timeout(2):
                        while snapshot()["presence"] != "unavailable":
                            await asyncio.sleep(.02)
                    assert not stopping.done(), "detection survived until recorder confirmation"
                    await stopping
                finally:
                    stopping.cancel()
                    await asyncio.gather(stopping, return_exceptions=True)
            else:
                await write(True)
            stop_ms = round((time.monotonic()-start_stop)*1000, 2)
            assert stop_ms >= args.minimum_stop_ms
            final = gateway.path()
            assert final["source"]["id"] == active["source"]["id"] == initial["source"]["id"]
            private = snapshot()
            assert private["presence"] == "unavailable" and private["source"] is None
            assert private["source_session_id"] is None
            assert detections.publish(source, False) == 503
            await reachability(False)
            assert await attribute(admin, node, "3/1030/0") == 1
            frozen = archive()
            await asyncio.sleep(1)
            assert frozen and archive() == frozen
            device.kill()
            device.wait(timeout=5)
            device = boot("privacy-restart")
            await read(True)
            await asyncio.sleep(3.2)
            assert recording(gateway.path(), False) and archive() == frozen
            gateway.revoke("matter_recording")
            assert request(internal, "GET", CONTROL_PATH, recorder_token)[0] == 403
            refusal = await admin.request("write_attribute", node_id=node, attribute_path=RECORDING_PRIVACY, value=False)
            assert [status["Status"] for status in refusal] == [156]  # Matter Busy
            assert recording(gateway.path(), False) and archive() == frozen
            assert all(p.poll() is None for p in (*children, gateway_process, server, device))
            frames = 0
            for clip in frozen:
                with av.open(str(clip)) as content:
                    frames += sum(1 for _ in content.decode(video=0))
            assert frames >= 30, "recorded pictures did not independently decode"
            return dict(decoded_frames=frames, stop_ms=stop_ms, durable_allow_and_deny=True,
                        same_source=True, wrong_camera_and_revoked_identity_refused=True,
                        occupied_sensor_became_unavailable=True, cached_detection_fenced=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "device", "controller", "gateway", "publisher", "mediamtx", "sdk"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--ip", required=True)
    parser.add_argument("--minimum-stop-ms", type=float, default=0,
                        help="minimum observed stop time when running with an injected native delay")
    arguments = parser.parse_args()
    print(json.dumps(asyncio.run(run(arguments))), flush=True)
