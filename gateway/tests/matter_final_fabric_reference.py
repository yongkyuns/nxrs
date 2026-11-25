#!/usr/bin/env python3
"""Remove the last Matter fabric from an isolated camera and recommission it."""

import argparse
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import struct

from matter_commissioning_reference import provision
from matter_fabric_reference import attribute, controller, fabric_ids
from matter_gateway_reference import start
from matter_recovery_reference import interrupt
from matter_reference import measured_peer, playback
from matter_rtsp_reference import ports, publish, start_media, wait_path
from matter_session_reference import selected_port, socket_owned
from matter_source_access import private_file

LABEL = "0/40/5"
MUTED = "1/1361/29"
SESSIONS = "1/1363/0"
IDENTITY = ("1/57/18", "3/57/18")


def record_keys(state):
    # Inspect only record headers from the isolated test node's own store.
    data = state.read_bytes()
    assert data[:8] == b"NXMAT\0\0\x01"
    position = 18
    count = struct.unpack_from("<H", data, 16)[0]
    keys = set()
    for _ in range(count):
        key, size = struct.unpack_from("<HI", data, position)
        keys.add(key)
        position += 6 + size
    assert len(keys) == count and position == len(data) - 32
    return keys


async def ready_playback(c, node):
    # The RTSP reader registers before its first validated keyframe.
    async with asyncio.timeout(12):
        while True:
            try:
                return await playback(c, node, 1, "camera-offer", 45)
            except RuntimeError as error:
                if "(Matter status 203)" not in str(error):
                    raise
                await asyncio.sleep(0.15)


async def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    with socket.socket(type=socket.SOCK_DGRAM) as probe:
        probe.bind((args.ip, 5540))
    factory = provision(root / "factory", args.controller)
    code = json.loads((factory.parent / "onboarding.json").read_bytes())["qrCode"]
    active = root / "state" / "state.bin"

    with ExitStack() as all_services:
        gateway, _, _, rtsp = start_media(all_services, root, args)
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
                    state_directory=str(active.parent),
                    snapshot_ffmpeg=str(args.ffmpeg.resolve(strict=True)),
                )
            ),
        )
        publisher = publish(all_services, root, args, rtsp, gateway.publisher, "red", "640x360", 15)

        def boot(stack, folder, *flags):
            folder.mkdir(mode=0o700)
            device = start(
                stack,
                folder,
                "matter",
                [args.device, "--config", config, *flags],
                dict(os.environ, RUST_LOG="error", TMPDIR=str(folder)),
            )
            wait_path(gateway, lambda p: p.get("ready") and len(p["readers"]) == 1, (*publisher, device))
            return device

        async def pair(c):
            result = await c.request(
                "commission_with_code", code=code, network_only=True, discovery_timeout_s=60
            )
            return result["node_id"]

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

        async def retired(c, node):
            try:
                async with asyncio.timeout(3):
                    await attribute(c, node, "0/62/3")
            except TimeoutError:
                return
            except RuntimeError as error:
                assert "controller error " in str(error)
                return
            raise AssertionError("removed administrator retained operational access")

        first = root / "first"
        with ExitStack() as stack:
            device = boot(stack, first, "--initialize-state", "--open-commissioning-window")
            async with controller(stack, first, args, "retiring", ports(1)[0], 32716) as admin:
                node = await pair(admin)
                index = await attribute(admin, node, "0/62/5")
                defaults = [await attribute(admin, node, path) for path in (LABEL, MUTED)]
                identities = [await attribute(admin, node, path) for path in IDENTITY]
                assert defaults[1] is True
                for path, value in ((LABEL, "Previous Matter installation"), (MUTED, False)):
                    status = await admin.request(
                        "write_attribute", node_id=node, attribute_path=path, value=value
                    )
                    assert status[0]["Status"] == 0 and await attribute(admin, node, path) == value
                async with asyncio.timeout(8):
                    while not any(0x800 <= key < 0x1000 for key in record_keys(active)):
                        await asyncio.sleep(0.05)
                if not args.breakpoint:
                    async with controller(stack, first, args, "other", ports(1)[0], 32719) as other:
                        ticket = await admin.request(
                            "open_commissioning_window", node_id=node, timeout=180, iteration=1000, option=1
                        )
                        pair_other = await other.request(
                            "commission_on_network",
                            setup_pin_code=ticket["setup_pin_code"],
                            ip_addr=args.ip,
                            discovery_timeout_s=60,
                        )
                        other_index = await attribute(other, pair_other["node_id"], "0/62/5")
                        assert other_index != index
                        await remove(admin, node, other_index)
                        assert await fabric_ids(admin, node) == {32716: index}
                        assert await attribute(admin, node, LABEL) == "Previous Matter installation"
                        assert await attribute(admin, node, MUTED) is False
                        assert {index, 256, 4096} <= record_keys(active)
                    invite = await admin.request(
                        "open_commissioning_window", node_id=node, timeout=180, iteration=1000, option=1
                    )
                    assert await attribute(admin, node, "0/60/0") == 1

                async with measured_peer(admin, node, 1, "receiver-offer", 45, request_end=False) as (
                    live,
                    peer,
                    _,
                ):
                    port = selected_port(peer, args.ip)
                    assert socket_owned(args.ip, port)
                    if args.breakpoint:
                        await interrupt(
                            stack,
                            first,
                            args,
                            device,
                            args.breakpoint,
                            "::handle_remove_fabric",
                            lambda: remove(admin, node, index),
                            remove_key=index,
                        )
                        assert (active.parent / "state.next").is_file() == (
                            args.breakpoint == "before-replace"
                        )
                    else:
                        await remove(admin, node, index)
                        async with asyncio.timeout(5):
                            while socket_owned(args.ip, port):
                                await asyncio.sleep(0.025)
                        await retired(admin, node)
                    assert not socket_owned(args.ip, port)
            if not args.breakpoint:
                async with controller(stack, first, args, "old-invite", ports(1)[0], 32718) as guest:
                    try:
                        async with asyncio.timeout(12):
                            await guest.request(
                                "commission_on_network",
                                setup_pin_code=invite["setup_pin_code"],
                                ip_addr=args.ip,
                                discovery_timeout_s=8,
                            )
                    except (TimeoutError, RuntimeError):
                        assert device.poll() is None, "window closed by process exit"
                    else:
                        raise AssertionError("invitation remained usable after removal of its last fabric")
        wait_path(gateway, lambda p: p.get("ready") and not p["readers"], publisher)

        recovered = root / "recovered"
        unchanged_before_rename = args.breakpoint == "before-replace"
        if unchanged_before_rename:
            with ExitStack() as stack:
                boot(stack, recovered)
                async with controller(
                    stack, recovered, args, "old", ports(1)[0], 32716, existing_storage=first / "retiring"
                ) as admin:
                    assert await fabric_ids(admin, node) == {32716: index}
                    assert await attribute(admin, node, LABEL) == "Previous Matter installation"
                    assert await attribute(admin, node, MUTED) is False
                    assert (await ready_playback(admin, node))["decoded_frames"] == 45
                    await remove(admin, node, index)

        keys = record_keys(active)
        assert not any(1 <= key <= 254 for key in keys)
        assert not keys & {256, 4096, 4097, 4098}, "departed installation settings retained on disk"
        assert not any(0x800 <= key < 0x1000 for key in keys), "departed subscription records retained"
        assert {4099, 4100} <= keys, "device onboarding or endpoint identities removed"

        renewed = root / "renewed"
        with ExitStack() as stack:
            boot(stack, renewed, "--open-commissioning-window")
            async with controller(stack, renewed, args, "new", ports(1)[0], 32717) as admin:
                successor = await pair(admin)
                assert await fabric_ids(admin, successor) == {
                    32717: await attribute(admin, successor, "0/62/5")
                }
                assert [await attribute(admin, successor, path) for path in IDENTITY] == identities
                assert [await attribute(admin, successor, path) for path in (LABEL, MUTED)] == defaults
                assert not await attribute(admin, successor, SESSIONS)
                (acl,) = await attribute(admin, successor, "0/31/0")
                assert acl["1"] == 5
                new_stream = await ready_playback(admin, successor)

        return dict(
            interruption=args.breakpoint,
            pre_commit_installation_recovered=unchanged_before_rename,
            separate_controller_commissioned=True,
            persisted_settings_and_subscriptions_removed=True,
            device_and_endpoint_identity_preserved=True,
            default_privacy_restored=True,
            selective_removal_preserved_settings=True if args.breakpoint is None else None,
            old_invitation_rejected=True if args.breakpoint is None else None,
            live_socket_released=True,
            decoded_frames_before_removal=live["decoded_frames"],
            decoded_frames_after_recommissioning=new_stream["decoded_frames"],
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "device", "controller", "gateway", "publisher", "mediamtx", "ffmpeg"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--ip", required=True)
    parser.add_argument("--lldb", type=Path)
    parser.add_argument("--breakpoint", choices=("before-replace", "after-sync"))
    args = parser.parse_args()
    if args.breakpoint and not args.lldb:
        parser.error("--breakpoint needs --lldb")
    result = asyncio.run(run(args))
    (args.runtime / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
