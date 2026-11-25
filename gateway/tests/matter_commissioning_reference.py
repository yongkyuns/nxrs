#!/usr/bin/env python3
"""Verify operator-initiated commissioning against a private, independent controller."""

import argparse
import asyncio
import base64
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import time

from matter_audio_reference import publish_av
from matter_fabric_reference import attribute, controller
from matter_gateway_reference import stop
from matter_reference import measured_peer
from matter_rtsp_reference import ports, start_device, start_media, wait_path


def provision(directory, controller_bin):
    tool = Path(__file__).with_name("matter") / "provision.cjs"
    install = controller_bin.absolute().parent.parent.parent
    subprocess.run(["node", tool, install, directory], check=True, timeout=15)
    return directory / "factory.json"


async def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    state = root / "state"
    record = provision(root / "device-material", args.controller)
    other = provision(root / "replacement", args.controller)
    discriminator = json.loads(record.read_bytes())["discriminator"]
    ticket = json.loads((record.parent / "onboarding.json").read_bytes())
    with socket.socket(type=socket.SOCK_DGRAM) as probe:
        probe.bind((args.ip, 5540))
    with ExitStack() as stack:
        gateway, _, _, rtsp = start_media(stack, root, args)
        publishers = publish_av(stack, root, args, rtsp, gateway.publisher)

        def boot(stage, *, initialize=False, window=False, data=record):
            folder = root / stage
            folder.mkdir(mode=0o700)
            flags = ["--factory-data", data]
            if window:
                flags.append("--open-commissioning-window")
            return start_device(
                stack, folder, args, gateway, rtsp, state, initialize=initialize, extra_args=flags
            )

        def serving(process):
            wait_path(gateway, lambda p: p.get("ready") and len(p["readers"]) == 1, (*publishers, process))

        def refused(process):
            assert process.wait(timeout=8) != 0, "gateway accepted invalid startup"

        async with controller(stack, root, args, "operator", ports(1)[0], 32705) as admin:

            async def advertised(observer=admin):
                nodes = await observer.request("discover_commissionable_nodes")
                return any(
                    row.get("long_discriminator") == discriminator and row.get("commissioning_mode") == 1
                    for row in nodes
                )

            async def fresh_advertisement(stage, fabric):
                with ExitStack() as process:
                    async with controller(process, root, args, stage, ports(1)[0], fabric) as observer:
                        return await advertised(observer)

            async def until_advertised(wanted, timeout):
                async with asyncio.timeout(timeout):
                    while await advertised() != wanted:
                        await asyncio.sleep(0.3)

            invalid = root / "invalid-point.json"
            fields = json.loads(record.read_bytes())
            original = base64.b64decode(fields["verifier"])
            fields["verifier"] = base64.b64encode(original[:32] + bytes([4]) + bytes(64)).decode()
            with invalid.open("x") as file:
                os.fchmod(file.fileno(), 0o600)
                json.dump(fields, file)
            refused(boot("invalid-point", initialize=True, data=invalid))
            assert not state.exists(), "invalid factory input initialized durable state"
            device = boot("initial", initialize=True)
            serving(device)
            assert not await advertised(), "initialization opened a commissioning window"
            stop(device)

            # A real 180-second SDK window, using the normal clock and multicast discovery.
            opened = time.monotonic()
            device = boot("expires", window=True)
            serving(device)
            await until_advertised(True, 30)
            await asyncio.sleep(max(0, opened + 165 - time.monotonic()))
            assert await advertised(), "window closed prematurely"
            await asyncio.sleep(max(0, opened + 181 - time.monotonic()))
            # A new Matter Server has no prior commissionable-node discovery cache.
            assert not await fresh_advertisement("observer", 32706), (
                "expired device still advertises to a fresh controller"
            )
            elapsed = time.monotonic() - opened
            assert 180 <= elapsed <= 216 and device.poll() is None
            cached_after_expiry = await advertised()
            try:
                await admin.request(
                    "commission_with_code", code=ticket[args.code], network_only=True, discovery_timeout_s=6
                )
            except RuntimeError as error:
                if "controller error" not in str(error):
                    raise
            else:
                raise AssertionError("expired window admitted commissioning")
            assert not await admin.request("get_nodes")

            stop(device)

            device = boot("uncommissioned-restart")
            serving(device)
            assert not await fresh_advertisement("empty-boot-observer", 32707)
            stop(device)
            refused(boot("different-factory", data=other))
            os.chmod(record, 0o644)
            try:
                refused(boot("public-factory"))
            finally:
                os.chmod(record, 0o600)

            device = boot("commission", window=True)
            serving(device)
            await until_advertised(True, 30)
            paired = await admin.request(
                "commission_with_code", code=ticket[args.code], network_only=True, discovery_timeout_s=60
            )
            node = paired["node_id"]
            assert await attribute(admin, node, "0/60/0") == 0
            async with measured_peer(admin, node, 1, "receiver-offer", 30) as (first, _, _):
                pass
            device.kill()
            device.wait(timeout=8)
            refused(boot("commissioned-local-window", window=True))
            device = boot("commissioned-restart")
            serving(device)
            assert await attribute(admin, node, "0/60/0") == 0
            assert not await fresh_advertisement("fabric-boot-observer", 32708)
            async with measured_peer(admin, node, 1, "camera-offer", 30) as (second, _, _):
                pass
            await admin.request("remove_node", node_id=node)
            assert not await admin.request("get_nodes")
            assert not await fresh_advertisement("removed-observer", 32709), (
                "removing the last fabric opened a local window"
            )
            stop(device)
            device = boot("open-after-removal", window=True)
            serving(device)
            other_code = "manualCode" if args.code == "qrCode" else "qrCode"
            again = await admin.request(
                "commission_with_code", code=ticket[other_code], network_only=True, discovery_timeout_s=60
            )
            assert await attribute(admin, again["node_id"], "0/60/0") == 0
            return dict(
                second_onboarding_method=other_code,
                last_fabric_removal_closed=True,
                expired_window_denied_commissioning=True,
                original_controller_still_listed_expired=cached_after_expiry,
                onboarding_method=args.code,
                window_seconds=180,
                closed_observed_seconds=round(elapsed, 2),
                invalid_curve_point_rejected_before_state_creation=True,
                initialization_closed=True,
                empty_restart_closed=True,
                commissioned_restart_closed=True,
                replacement_and_world_readable_factory_rejected=True,
                commissioned_local_open_rejected=True,
                commissioned_with_independent_verifier=True,
                decoded_frames=[first["decoded_frames"], second["decoded_frames"]],
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "device", "controller", "gateway", "publisher", "mediamtx"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--ip", required=True)
    parser.add_argument("--code", choices=("qrCode", "manualCode"), default="qrCode")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    (args.runtime / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
