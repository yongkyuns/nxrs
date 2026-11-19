#!/usr/bin/env python3
"""Run an isolated Nxrs bridge for manual Matter commissioning on a phone."""

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import subprocess
import sys
import time

from matter_attestation_reference import public_test_bundle
from matter_audio_reference import publish_av
from matter_commissioning_reference import provision
from matter_gateway_reference import DetectionPublisher, start
from matter_rtsp_reference import start_media, wait_path
from matter_source_access import private_file


def run(args):
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required on PATH")
    device = args.device.resolve(strict=True)
    with socket.socket(type=socket.SOCK_DGRAM) as probe:
        probe.bind((args.ip, 5540))
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    factory = provision(root / "factory", args.controller)
    attestation = root / "attestation"
    public_test_bundle(args.sdk, attestation)
    enrolled = root / "enrollment"

    def configure(gateway):
        stager = Path(__file__).resolve().parents[1] / "tools/stage_matter.py"
        subprocess.run(
            [sys.executable, stager, gateway.config, enrolled, "--camera", "cam", "--identity", "bridge"],
            check=True, stdout=subprocess.DEVNULL, timeout=15,
        )
        gateway.config = enrolled / "control.json"

    with ExitStack() as children:
        gateway, server, control, rtsp = start_media(children, root, args, configure=configure)
        media = publish_av(children, root, args, rtsp, gateway.publisher)
        running = (server, control, *media)
        wait_path(gateway, lambda path: path.get("ready"), running)
        detection = DetectionPublisher(gateway.internal, "cam", "publisher", gateway.publisher)
        session = detection.register(control)
        deployment = root / "matter.json"
        private_file(deployment, json.dumps(dict(
            version=1, site_id="lab", camera_id="cam", host_power_source="dc",
            control=f"127.0.0.1:{gateway.control}", rtsp=f"127.0.0.1:{rtsp}",
            ice_address=args.ip, lan_interface=args.interface,
            media_token_file=str(enrolled / "media.token"),
            detection_token_file=str(enrolled / "detections.token"),
            factory_data=str(factory), attestation_directory=str(attestation),
            state_directory=str(root / "state"), snapshot_ffmpeg=str(Path(ffmpeg).resolve()),
        )))
        begin = time.monotonic()
        bridge = start(
            children, root, "matter", [device, "--config", deployment, "--allow-test-attestation",
                                       "--initialize-state", "--open-commissioning-window"],
            dict(os.environ, RUST_LOG="error"), cwd=root,
        )
        running = (*running, bridge)
        wait_path(gateway, lambda path: len(path.get("readers", [])) == 1, running)
        remaining = max(0, int(180 - (time.monotonic() - begin)))
        print(f"Pairing open for about {remaining} seconds. Find the setup code in {factory.with_name('onboarding.json')}.",
              flush=True)
        commands = "Enter clear, occupied, unavailable or quit."
        print(commands, flush=True)

        level = "clear"
        while all(process.poll() is None for process in running):
            if level != "unavailable":
                status = detection.publish(session, level == "occupied")
                if status != 204:
                    raise RuntimeError(f"fixture detection was rejected with HTTP {status}")
            if select.select([sys.stdin], [], [], 0.5)[0]:
                line = sys.stdin.readline()
                command = line.strip()
                if not line or command == "quit":
                    return
                if command in ("clear", "occupied", "unavailable"):
                    level = command
                    print(f"State set to {level}.", flush=True)
                else:
                    print(commands, flush=True)
        raise RuntimeError("fixture process exited")


def interrupt(_signal, _frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "sdk", "device", "controller", "gateway", "publisher", "mediamtx"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--ip", required=True)
    signal.signal(signal.SIGTERM, interrupt)
    try:
        run(parser.parse_args())
    except KeyboardInterrupt:
        pass
