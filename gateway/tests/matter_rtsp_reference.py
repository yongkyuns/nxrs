#!/usr/bin/env python3
"""Qualify live, shared RTSP media through a commissioned Matter test bridge and HA."""
import argparse
import asyncio
from contextlib import ExitStack
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

import aiohttp

from matter_gateway_reference import start, stop


def ports():
    with ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in range(2)]
        for s in sockets:
            s.bind(("127.0.0.1", 0))
        return [s.getsockname()[1] for s in sockets]


def get_path(api):
    conn = http.client.HTTPConnection("127.0.0.1", api, timeout=1)
    try:
        conn.request("GET", "/v3/paths/get/cam")
        res = conn.getresponse()
        data = json.loads(res.read(16384))
        return data if res.status == 200 else {}
    except ConnectionRefusedError:
        return {}
    finally:
        conn.close()


def wait_path(api, predicate, children=()):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        assert all(child.poll() is None for child in children), "fixture process exited"
        value = get_path(api)
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
    rtsp, api = ports()
    pub, read = secrets.token_hex(32), secrets.token_hex(32)
    def principal(user, password, permissions):
        return dict(user=user, **{"pass": password}, ips=["127.0.0.1"], permissions=permissions)
    cfg = dict(logLevel="warn", rtspAddress=f"127.0.0.1:{rtsp}", rtspTransports=["tcp"],
               rtspAuthMethods=["basic"], api=True, apiAddress=f"127.0.0.1:{api}",
               rtmp=False, hls=False, webrtc=False, srt=False, moq=False, playback=False,
               authInternalUsers=[principal("publisher", pub, [dict(action="publish", path="cam")]),
                                  principal("reader", read, [dict(action="read", path="cam")]),
                                  principal("any", "", [dict(action="api")])], paths={"cam": {}})
    config, credential = root/"mediamtx.json", root/"reader.token"
    for path, text in [(config, json.dumps(cfg)), (credential, read)]:
        with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as file:
            file.write(text)
    result = []
    with ExitStack() as stack:
        server = start(stack, root, "mtx", [args.mediamtx.resolve(), config], cwd=root)
        deadline = time.monotonic() + 10
        while True:
            try:
                with socket.create_connection(("127.0.0.1", rtsp), timeout=1):
                    break
            except ConnectionRefusedError:
                assert server.poll() is None and time.monotonic() < deadline
                time.sleep(.05)
        env = dict(os.environ, WEBRTC_CAMERA_TRIAL_DIR=str(root), TMPDIR=str(root),
                   WEBRTC_CAMERA_STATE=str(args.state.resolve()), WEBRTC_RTSP_ADDR=f"127.0.0.1:{rtsp}",
                   WEBRTC_RTSP_PATH="cam", WEBRTC_RTSP_USER="reader", WEBRTC_RTSP_TOKEN_FILE=str(credential),
                   RUST_LOG="error")
        for key in ("WEBRTC_CAMERA_H264", "WEBRTC_CAMERA_CONTROL"):
            env.pop(key, None)
        device = start(stack, root, "matter", [args.device], env)
        for color, size, rate, count in [("red", "640x360", 15, 2), ("blue", "320x240", 10, 1)]:
            with ExitStack() as publication:
                encoder, publisher = publish(publication, root, args, rtsp, pub, color, size, rate)
                active = wait_path(api, lambda p: len(p.get("readers", [])) == 1 and p.get("ready"),
                                   (server, device, encoder, publisher))
                reader_id = active["readers"][0]["id"]
                assert active["source"]["type"] == active["readers"][0]["type"] == "rtspSession"
                views = []
                signals = []
                source_stopped = False
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
                        path = get_path(api)
                        assert path.get("ready") and [r["id"] for r in path["readers"]] == [reader_id]
                        samples.append(time.time_ns() / 1e6)
                        if color == "red" and all(p.exists() for p in signals):
                            assert asyncio.run(session_count()) == count, "active Matter session count"
                            publication.close()
                            source_stopped = True
                    time.sleep(.2)
                assert all(proc.returncode == 0 for proc, _ in views), "camera dialog failed"
                reports = [json.loads(path.read_text()) for _, path in views]
                assert all(r["dimensions"] == list(map(int, size.split("x"))) for r in reports)
                begin = max(r["first_presented_ms"] for r in reports)
                end = min(r["last_presented_ms"] for r in reports)
                assert any(begin < at < end for at in samples), "no shared-reader sample during presentation"
                result.append(dict(marker=color, source_size=size, source_fps=rate, concurrent_views=count,
                                   overlap_seconds=round((end-begin)/1000, 2), upstream_readers=1,
                                   presented_frames=[r["presented_frames"] for r in reports],
                                   presented_fps=[r["presented_fps"] for r in reports],
                                   decoded_clock_min_step_90khz=[r["min_step_90khz"] for r in reports]))
                if color == "red":
                    assert source_stopped and all(r["peer_ended_before_dialog_closed"] for r in reports)
                    result[-1]["active_views_ended_on_source_loss"] = count
                    deadline = time.monotonic() + 5
                    while (remaining := asyncio.run(session_count())) > 0 and time.monotonic() < deadline:
                        time.sleep(.2)
                    assert remaining == 0, f"source loss retained {remaining} Matter sessions"
                    result[-1]["remaining_matter_sessions"] = remaining
                if color == "blue":
                    stop(device)
                    released = wait_path(api, lambda p: p.get("ready") and not p.get("readers"))
                    assert released["source"]["id"] == active["source"]["id"]
                    result[-1]["reader_released_while_publisher_live"] = True
            wait_path(api, lambda p: not p.get("ready") and not p.get("readers"))
        stop(device)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "state", "mediamtx", "publisher", "device", "storage-state"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--camera", required=True)
    args = parser.parse_args()
    (args.runtime / "result.json").write_text(json.dumps(run(args), indent=2) + "\n")
    print("Live RTSP source sharing, browser markers and replacement passed")
