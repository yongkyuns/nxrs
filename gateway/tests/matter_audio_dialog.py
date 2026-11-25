#!/usr/bin/env python3
"""Present one paired publication through a commissioned, isolated Home Assistant camera."""
import argparse
import asyncio
from contextlib import ExitStack, suppress
import json
from pathlib import Path
import time

import aiohttp

from matter_audio_allocation import CLUSTER, LIVE_OPUS
from matter_audio_reference import publish_av
from matter_reference import Controller
from matter_rtsp_reference import start, start_device, start_media, wait_path


async def microphone_ready():
    async with aiohttp.ClientSession() as http, http.ws_connect("http://127.0.0.1:15580/ws") as ws:
        await ws.receive_json(timeout=5)
        client = Controller(ws)
        pump = asyncio.create_task(client.read())
        try:
            nodes = await client.request("start_listening")
            node, = [n for n in nodes if "1/1363/0" in n["attributes"]]
            target = dict(node_id=node["node_id"], endpoint_id=1, cluster_id=CLUSTER)
            async with asyncio.timeout(45):
                while True:
                    try:
                        reply = await client.request("device_command", **target, command_name="AudioStreamAllocate",
                                                     payload=LIVE_OPUS)
                    except RuntimeError as error:
                        if "controller error " not in str(error):
                            raise
                        await asyncio.sleep(.25)
                        continue
                    await client.request("device_command", **target, command_name="AudioStreamDeallocate",
                                         payload=dict(audioStreamID=reply["audioStreamID"]))
                    return
        finally:
            pump.cancel()
            with suppress(asyncio.CancelledError):
                await pump


def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    with ExitStack() as stack:
        gateway, _, _, port = start_media(stack, root, args)
        device = start_device(stack, root, args, gateway, port, args.state)
        children = publish_av(stack, root, args, port, gateway.publisher)
        active = wait_path(gateway, lambda p: p.get("ready") and len(p["readers"]) == 1, (*children, device))
        asyncio.run(microphone_ready())
        output = root / "result.json"
        browser = start(stack, root, "browser", [args.browser_python, Path(__file__).with_name("matter_browser_reference.py"),
            "--storage-state", args.storage_state, "--entity", args.camera, "--output", output,
            "--fixture-rate", "15", "--audio-cue"])
        deadline = time.monotonic() + 105
        while browser.poll() is None:
            assert time.monotonic() < deadline and device.poll() is None, "browser deadline or device exit"
            path = gateway.path()
            assert path.get("ready") and path["readers"] == active["readers"], "authorized camera reader changed"
            time.sleep(.2)
        assert browser.returncode == 0, "Home Assistant audio/video witness failed"
        result = json.loads(output.read_text())
        assert result["dimensions"] == [320,240] and result["audio_cue"]["shared_timing_context"]
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "gateway", "mediamtx", "publisher", "device", "state", "browser-python", "storage-state"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--camera", required=True)
    print(json.dumps(run(parser.parse_args())))
