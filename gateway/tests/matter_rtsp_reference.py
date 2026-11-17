#!/usr/bin/env python3
"""Qualify live, shared RTSP media through a commissioned Matter test bridge and HA."""
import argparse
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import aiohttp

from matter_gateway_reference import start, stop
from matter_source_access import StrictMedia, private_file


def ports(count):
    with ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in range(count)]
        for s in sockets:
            s.bind(("127.0.0.1", 0))
        return [s.getsockname()[1] for s in sockets]


def wait_path(gateway, predicate, children=()):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        assert all(child.poll() is None for child in children), "fixture process exited"
        value = gateway.path()
        if predicate(value):
            return value
        time.sleep(.1)
    raise TimeoutError("MediaMTX path condition")


async def session_count():
    path = "1/1363/0"  # WebRTC Transport Provider / CurrentSessions.
    async with aiohttp.ClientSession() as http, http.ws_connect("http://127.0.0.1:15580/ws") as ws:
        await ws.receive_json(timeout=5)
        async def command(identifier, command, **args):
            await ws.send_json(dict(message_id=identifier, command=command, args=args))
            async with asyncio.timeout(5):
                while True:
                    event = await ws.receive_json()
                    if event.get("message_id") == identifier:
                        return event["result"]
        nodes = await command("1", "start_listening")
        assert len(nodes) == 1
        data = await command("2", "read_attribute", node_id=nodes[0]["node_id"], attribute_path=path)
        assert isinstance(data[path], list)
        return len(data[path])


def publish(stack, root, args, rtsp, token, color, size, rate):
    # The publisher consumes credentials from stdin. The encoder inherits the
    # same pipe after the preamble and only ever sees public media arguments.
    with ExitStack() as pipes:
        read, write = os.pipe()
        inp = pipes.enter_context(os.fdopen(read, "rb", buffering=0))
        out = pipes.enter_context(os.fdopen(write, "wb", buffering=0))
        out.write(token.encode())
        with (root / "publisher.log").open("ab") as log:
            pub = subprocess.Popen([args.publisher, f"127.0.0.1:{rtsp}", "publisher", "cam"],
                                   stdin=inp, stdout=log, stderr=log)
            stack.callback(stop, pub)
        with (root / "encoder.log").open("ab") as log:
            encoder = subprocess.Popen(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-re", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}",
                "-vf", f"drawbox=x=0:y=0:w=iw/4:h=ih/4:color={color}:t=fill",
                "-c:v", "libx264", "-threads", "2", "-profile:v", "baseline",
                "-preset", "ultrafast", "-tune", "zerolatency", "-bf", "0", "-g", str(rate),
                "-f", "mpegts", "pipe:1"], stdout=out, stderr=log)
            stack.callback(stop, encoder)
    return encoder, pub


def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    rtsp, api, control, internal, whep, ice = ports(6)
    gateway = StrictMedia(root, api, control, internal)
    config = root / "mediamtx.json"
    private_file(config, json.dumps(dict(logLevel="warn", rtspAddress=f"127.0.0.1:{rtsp}", rtspTransports=["tcp"],
        rtspAuthMethods=["basic"], api=True, apiAddress=f"127.0.0.1:{api}", rtmp=False, hls=False, srt=False,
        moq=False, playback=False, webrtc=True, webrtcAddress=f"127.0.0.1:{whep}", webrtcLocalUDPAddress=f"127.0.0.1:{ice}",
        webrtcIPsFromInterfaces=False, webrtcAdditionalHosts=["127.0.0.1"], webrtcICEServers2=[],
        authMethod="http", authHTTPExclude=[], authHTTPAddress=f"http://127.0.0.1:{internal}/internal/mediamtx/auth",
        paths={"cam":{"overridePublisher":False, "maxReaders":8}})))
    result = []
    with ExitStack() as stack:
        def start_control(revoked=False):
            process = start(stack, root, "gateway", [args.gateway, gateway.config, "--security", gateway.security])
            gateway.ready(process, revoked)
            return process

        server = start(stack, root, "mtx", [args.mediamtx.resolve(), config], cwd=root)
        deadline = time.monotonic() + 10
        while True:
            try:
                with socket.create_connection(("127.0.0.1", rtsp), timeout=1):
                    break
            except ConnectionRefusedError:
                assert server.poll() is None and time.monotonic() < deadline
                time.sleep(.05)
        controller = start_control()
        env = dict(os.environ, WEBRTC_CAMERA_TRIAL_DIR=str(root), TMPDIR=str(root),
                   WEBRTC_CAMERA_STATE=str(args.state.resolve()), WEBRTC_RTSP_ADDR=f"127.0.0.1:{rtsp}",
                   WEBRTC_RTSP_PATH="cam", WEBRTC_RTSP_USER="reader", WEBRTC_RTSP_TOKEN_FILE=str(gateway.credentials["reader"]),
                   RUST_LOG="error")
        for key in ("WEBRTC_CAMERA_H264", "WEBRTC_CAMERA_CONTROL"):
            env.pop(key, None)
        device = start(stack, root, "matter", [args.device], env)
        for color, size, rate, count in [("red", "640x360", 15, 2), ("blue", "320x240", 10, 1)]:
            with ExitStack() as publication:
                encoder, publisher = publish(publication, root, args, rtsp, gateway.publisher, color, size, rate)
                active = wait_path(gateway, lambda p: len(p.get("readers", [])) == 1 and p.get("ready"),
                                   (server, device, encoder, publisher))
                reader_id = active["readers"][0]["id"]
                assert active["source"]["type"] == active["readers"][0]["type"] == "rtspSession"
                views = []
                signals = []
                source_stopped = False
                def start_receiver(name, suffix=""):
                    progress = root / (name + suffix + ".frames")
                    return start(stack, root, name, [args.receiver_python, Path(__file__).with_name("matter_source_access.py"),
                        str(rtsp), name, gateway.credentials[name], progress]), progress

                readers = []
                if color == "red" and args.fault == "revoke":
                    readers = [start_receiver(name) for name in ("reader", "independent")]
                    wait_path(gateway, lambda p: len(p.get("readers", [])) == 3 and
                              all(path.exists() and path.stat().st_size > 1 for _, path in readers))

                for i in range(count):
                    output = root/f"{color}-{i}.json"
                    signal = root/f"source-loss-{i}.ready"
                    signals.append(signal)
                    loss = ["--await-source-loss", signal] if color == "red" else []
                    proc = start(stack, root, f"browser-{color}-{i}",
                                 [sys.executable, str(Path(__file__).with_name("matter_browser_reference.py")),
                                  "--storage-state", args.storage_state, "--entity", args.camera,
                                  "--fixture-marker", color, "--fixture-rate", str(rate), "--output", output, *loss])
                    views.append((proc, output))
                deadline = time.monotonic() + 100
                samples = []
                while any(proc.poll() is None for proc, _ in views):
                    assert time.monotonic() < deadline and device.poll() is None
                    if not source_stopped:
                        path = gateway.path()
                        assert path.get("ready") and len(path["readers"]) == 1 + len(readers)
                        assert reader_id in {r["id"] for r in path["readers"]}
                        samples.append(time.time_ns() / 1e6)
                        if color == "red" and all(p.exists() for p in signals):
                            assert asyncio.run(session_count()) == count, "active Matter session count"
                            cut = time.monotonic()
                            if args.fault == "source":
                                publication.close()
                            elif args.fault == "control-loss":
                                stop(controller)
                            else:
                                gateway.revoke()
                            while asyncio.run(session_count()) and time.monotonic() - cut < 10:
                                time.sleep(.1)
                            end_delay = time.monotonic() - cut
                            assert end_delay <= 10 and asyncio.run(session_count()) == 0, "Matter retirement deadline"
                            source_stopped = True
                    time.sleep(.2)
                assert all(proc.returncode == 0 for proc, _ in views), "camera dialog failed"
                reports = [json.loads(path.read_text()) for _, path in views]
                assert all(r["dimensions"] == list(map(int, size.split("x"))) for r in reports)
                begin = max(r["first_presented_ms"] for r in reports)
                end = min(r["last_presented_ms"] for r in reports)
                assert any(begin < at < end for at in samples), "no shared-reader sample during presentation"
                result.append(dict(marker=color, source_size=size, source_fps=rate, concurrent_views=count,
                                   overlap_seconds=round((end-begin)/1000, 2), upstream_readers=1 + len(readers),
                                   presented_frames=[r["presented_frames"] for r in reports],
                                   presented_fps=[r["presented_fps"] for r in reports],
                                   decoded_clock_min_step_90khz=[r["min_step_90khz"] for r in reports]))
                if color == "red":
                    assert source_stopped and all(r["peer_ended_before_dialog_closed"] for r in reports)
                    result[-1].update(fault=args.fault, ended_views=count, retirement_seconds=round(end_delay, 2),
                                      remaining_matter_sessions=0)
                    if args.fault == "control-loss":
                        assert publisher.poll() is None and encoder.poll() is None
                        controller = start_control()
                        restored = wait_path(gateway, lambda p: len(p.get("readers", [])) == 1)
                        assert restored["source"]["id"] == active["source"]["id"]
                        assert restored["readers"][0]["id"] != reader_id
                        result[-1]["source_reauthenticated_after_control_restart"] = True
                    if args.fault == "revoke":
                        denied, other = readers
                        denied[0].wait(timeout=5)
                        after = wait_path(gateway, lambda p: len(p.get("readers", [])) == 1)
                        assert after["source"]["id"] == active["source"]["id"]
                        n = other[1].stat().st_size
                        wait_path(gateway, lambda p: p.get("ready") and other[1].stat().st_size >= n + 15)
                        result[-1]["independent_reader_advanced_after_revocation"] = True
                        stop(controller)
                        controller = start_control(revoked=True)
                        refused, progress = start_receiver("reader", "-retry")
                        assert refused.wait(timeout=8) == 77 and not progress.exists(), "expected RTSP 401"
                        accepted, progress = start_receiver("independent", "-retry")
                        restored = wait_path(gateway, lambda p: len(p.get("readers", [])) == 1
                                             and progress.exists() and progress.stat().st_size >= 15,
                                             (device, server, publisher, accepted))
                        assert restored["source"]["id"] == active["source"]["id"]
                        result[-1]["revoked_rtsp_status_after_restart"] = 401
                        result[-1]["independent_reader_reopened"] = True
                        return result
                if color == "blue":
                    stop(device)
                    released = wait_path(gateway, lambda p: p.get("ready") and not p.get("readers"))
                    assert released["source"]["id"] == active["source"]["id"]
                    result[-1]["reader_released_while_publisher_live"] = True
            wait_path(gateway, lambda p: not p.get("ready") and not p.get("readers"))
        stop(device)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "state", "mediamtx", "publisher", "device", "storage-state", "gateway"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--camera", required=True)
    parser.add_argument("--fault", choices=("source", "control-loss", "revoke"), default="source")
    parser.add_argument("--receiver-python", type=Path, help="Python with the pinned PyAV receiver for --fault revoke")
    args = parser.parse_args()
    if args.fault == "revoke" and args.receiver_python is None:
        parser.error("--fault revoke requires --receiver-python")
    (args.runtime / "result.json").write_text(json.dumps(run(args), indent=2) + "\n")
    print("Strict Matter media trial passed:", args.fault)
