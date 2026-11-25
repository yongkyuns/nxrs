#!/usr/bin/env python3
"""Kill a disposable Matter gateway during the durable CommissioningComplete write."""

import argparse
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import struct
import subprocess

from matter_commissioning_reference import provision
from matter_fabric_reference import attribute, controller, fabric_ids
from matter_final_fabric_reference import ready_playback
from matter_gateway_reference import start, stop
from matter_recovery_reference import interrupt
from matter_rtsp_reference import ports, publish, start_media, wait_path
from matter_source_access import private_file

# Reserved keys at the pinned SDK revision. Read only record headers and the
# one-byte fabric allocator cursor, never operational credentials.
NEXT_FABRIC_INDEX = 273
ADMIN_FABRIC = 32720
ATTEMPT_FABRIC = 32721
RECOVERY_FABRIC = 32722


def enrollment(path):
    data = path.read_bytes()
    assert data[:8] == b"NXMAT\0\0\x01"
    count = struct.unpack_from("<H", data, 16)[0]
    offset = 18
    indices, cursor = set(), None
    for _ in range(count):
        key, length = struct.unpack_from("<HI", data, offset)
        offset += 6
        if 1 <= key <= 254:
            indices.add(key)
        if key == NEXT_FABRIC_INDEX:
            cursor = data[offset : offset + length]
            assert len(cursor) <= 3
        offset += length
    assert offset == len(data) - 32
    return indices, cursor


async def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    with socket.socket(type=socket.SOCK_DGRAM) as probe:
        probe.bind((args.ip, 5540))
    factory = provision(root / "factory", args.controller)
    ticket = json.loads((factory.parent / "onboarding.json").read_bytes())
    state = root / "state"

    with ExitStack() as shared:
        gateway, _, _, rtsp = start_media(shared, root, args)
        config = root / "matter.json"
        private_file(
            config,
            json.dumps(
                dict(
                    version=1,
                    site_id="lab",
                    camera_id="cam",
                    ice_address=args.ip,
                    control=f"127.0.0.1:{gateway.control}",
                    rtsp=f"127.0.0.1:{rtsp}",
                    media_token_file=str(gateway.credentials["reader"]),
                    detection_token_file=str(gateway.credentials["independent"]),
                    factory_data=str(factory),
                    state_directory=str(state),
                    snapshot_ffmpeg=str(args.ffmpeg.resolve(strict=True)),
                )
            ),
        )
        publisher = publish(shared, root, args, rtsp, gateway.publisher, "red", "640x360", 15)

        def launch(stack, folder, *flags):
            return start(
                stack,
                folder,
                "matter",
                [args.device, "--config", config, *flags],
                dict(os.environ, RUST_LOG="error", TMPDIR=str(folder)),
            )

        def boot(stack, folder, *flags):
            folder.mkdir(mode=0o700)
            device = launch(stack, folder, *flags)
            path = wait_path(
                gateway, lambda p: p.get("ready") and len(p["readers"]) == 1, (*publisher, device)
            )
            return device, path["source"]["id"]

        async def pair(c, pin=None):
            if pin is None:
                return await c.request(
                    "commission_with_code", code=ticket["qrCode"], network_only=True, discovery_timeout_s=60
                )
            return await c.request(
                "commission_on_network", setup_pin_code=pin, ip_addr=args.ip, discovery_timeout_s=60
            )

        async def invitation(c, node):
            return (
                await c.request(
                    "open_commissioning_window", node_id=node, timeout=180, iteration=1000, option=1
                )
            )["setup_pin_code"]

        async def remove(c, node, index):
            result = await c.request(
                "device_command",
                node_id=node,
                endpoint_id=0,
                cluster_id=62,
                command_name="RemoveFabric",
                payload={"fabricIndex": index},
            )
            assert result["statusCode"] == 0

        async def maintenance(*flags, succeeds=True):
            result = await asyncio.to_thread(
                subprocess.run,
                [args.device, "--config", config, *flags],
                env=dict(os.environ, RUST_LOG="error"),
                capture_output=True,
                timeout=8,
            )
            assert (result.returncode == 0) is succeeds, f"unexpected result for {flags[0]}"
            return result.stdout

        before = root / "before"
        before_rename = args.boundary == "before-replace"
        old_frames = None
        with ExitStack() as stack:
            device, source = boot(stack, before, "--initialize-state", "--open-commissioning-window")
            old = None
            if args.existing_fabric:
                async with controller(stack, before, args, "admin", ports(1)[0], ADMIN_FABRIC) as admin:
                    old = (await pair(admin))["node_id"]
                    assert await fabric_ids(admin, old) == {ADMIN_FABRIC: 1}
                    pin = await invitation(admin, old)
            else:
                pin = None
            attempted_index = 2 if old is not None else 1
            original_records, original_cursor = enrollment(state / "state.bin")
            assert original_records == ({1} if old is not None else set())
            async with controller(stack, before, args, "attempt", ports(1)[0], ATTEMPT_FABRIC) as attempt:
                await interrupt(
                    stack,
                    before,
                    args,
                    device,
                    args.boundary,
                    "::handle_commissioning_complete",
                    lambda: pair(attempt, pin),
                    store_key=attempted_index,
                )
            assert (state / "state.next").is_file() == before_rename
            after_records, after_cursor = enrollment(state / "state.bin")
            assert after_records == (
                original_records if before_rename else original_records | {attempted_index}
            )
            assert (after_cursor == original_cursor) == before_rename, (
                "fabric allocator committed separately from enrollment"
            )
        wait_path(gateway, lambda p: p.get("ready") and not p["readers"], publisher)
        if old is not None and not before_rename:
            # A local recovery for an abandoned first enrollment must not
            # remove any fabric when another administrator still exists.
            await maintenance("--remove-sole-fabric", str(attempted_index), succeeds=False)
            assert enrollment(state / "state.bin") == (after_records, after_cursor)

        after = root / "after"
        stock_controller_recovered = None
        local_recovery = False
        new_frames = None
        with ExitStack() as stack:
            flags = ("--open-commissioning-window",) if before_rename and old is None else ()
            device, after_source = boot(stack, after, *flags)
            assert after_source == source and not (state / "state.next").exists()
            retry_pin = None
            if old is not None:
                async with controller(
                    stack, after, args, "admin", ports(1)[0], ADMIN_FABRIC, existing_storage=before / "admin"
                ) as admin:
                    expected = {ADMIN_FABRIC: 1}
                    if not before_rename:
                        expected[ATTEMPT_FABRIC] = attempted_index
                    assert await fabric_ids(admin, old) == expected
                    assert await attribute(admin, old, "0/60/0") == 0
                    old_frames = (await ready_playback(admin, old))["decoded_frames"]
                    if not before_rename:
                        await remove(admin, old, attempted_index)
                    retry_pin = await invitation(admin, old)
            if not before_rename and old is None:
                async with controller(
                    stack,
                    after,
                    args,
                    "attempt",
                    ports(1)[0],
                    ATTEMPT_FABRIC,
                    existing_storage=before / "attempt",
                ) as attempt:
                    nodes = await attempt.request("get_nodes")
                    stock_controller_recovered = bool(nodes)
                    if nodes:
                        assert len(nodes) == 1
                        new_frames = (await ready_playback(attempt, nodes[0]["node_id"]))["decoded_frames"]
                if not stock_controller_recovered:
                    await maintenance("--remove-sole-fabric", str(attempted_index), succeeds=False)
                    assert enrollment(state / "state.bin") == (after_records, after_cursor)
                    await asyncio.to_thread(stop, device)
                    await asyncio.to_thread(
                        wait_path, gateway, lambda p: p.get("ready") and not p["readers"], publisher
                    )
                    token = gateway.credentials["reader"]
                    withdrawn = root / "reader-withdrawn"
                    token.rename(withdrawn)
                    try:
                        observed = await maintenance("--fabric-status")
                        assert json.loads(observed) == {"fabric_indices": [attempted_index]}
                        await maintenance("--remove-sole-fabric", str(attempted_index + 1), succeeds=False)
                        assert enrollment(state / "state.bin") == (after_records, after_cursor)
                        await maintenance("--remove-sole-fabric", str(attempted_index))
                        observed = await maintenance("--fabric-status")
                        assert json.loads(observed) == {"fabric_indices": []}
                    finally:
                        withdrawn.rename(token)
                    assert enrollment(state / "state.bin") == (set(), after_cursor)
                    _, after_source = boot(stack, root / "recovered", "--open-commissioning-window")
                    assert after_source == source
                    local_recovery = True

            if new_frames is None:
                async with controller(stack, after, args, "recovery", ports(1)[0], RECOVERY_FABRIC) as admin:
                    node = (await pair(admin, retry_pin))["node_id"]
                    assert await attribute(admin, node, "0/60/0") == 0
                    expected = {RECOVERY_FABRIC: attempted_index + int(not before_rename)}
                    if old is not None:
                        expected[ADMIN_FABRIC] = 1
                    assert await fabric_ids(admin, node) == expected
                    new_frames = (await ready_playback(admin, node))["decoded_frames"]
        return dict(
            boundary=args.boundary,
            existing_fabric=args.existing_fabric,
            cursor_atomic=True,
            stock_controller_recovered=stock_controller_recovered,
            local_recovery=local_recovery,
            original_admin_frames=old_frames,
            recommissioned_frames=new_frames,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "device", "controller", "gateway", "publisher", "mediamtx", "ffmpeg", "lldb"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--ip", required=True)
    parser.add_argument("--boundary", choices=("before-replace", "after-sync"), required=True)
    parser.add_argument("--existing-fabric", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    (args.runtime / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
