#!/usr/bin/env python3
"""Interrupt a real Matter administrative write and inspect its externally visible effect after restart."""

import argparse
import asyncio
from contextlib import ExitStack, suppress
import json
import os
from pathlib import Path
import signal
import socket

from matter_audio_reference import publish_av
from matter_commissioning_reference import provision
from matter_fabric_reference import attribute, controller, fabric_ids
from matter_gateway_reference import start
from matter_reference import measured_peer
from matter_rtsp_reference import ports, start_device, start_media, wait_path
from matter_viewer_permissions import viewer


async def interrupt(stack, folder, args, device, boundary, operation, change, *, remove_key=None, store_key=None):
    source = Path(__file__).resolve().parents[1] / "matter/src/persistence.rs"
    module = Path(__file__).resolve().with_name("matter") / "commit_breakpoint.py"
    config = dict(pid=device.pid, source=str(source), boundary=boundary, operation=operation)
    if remove_key is not None:
        config["remove_key"] = remove_key
    if store_key is not None:
        config["store_key"] = store_key
    (folder / "breakpoint.json").write_text(json.dumps(config))
    debugger = start(stack, folder, "lldb", [
        args.lldb, "--no-lldbinit", "--batch", "--attach-pid", str(device.pid),
        "-o", "command script import " + json.dumps(str(module)),
        "-o", "script commit_breakpoint.arm(lldb.debugger)", "-o", "continue",
        "-o", "script commit_breakpoint.stop_at_commit(lldb.debugger)",
    ], dict(os.environ, NXRS_RECOVERY_TRIAL=str(folder)))

    async def marker(name):
        async with asyncio.timeout(30):
            while not (folder / name).is_file():
                assert debugger.poll() is None, f"debugger exited before {name}"
                await asyncio.sleep(0.03)

    await marker("armed")
    request = asyncio.create_task(change())
    try:
        await marker("hit")
        assert await asyncio.to_thread(debugger.wait, 10) == 0
        assert await asyncio.to_thread(device.wait, 10) == -signal.SIGKILL
        if request.done() and not request.cancelled() and request.exception() is None:
            raise AssertionError("controller acknowledged the interrupted write")
    finally:
        # The process stack also stops this Matter Server before the device is
        # restarted. Its pending write cannot be replayed into the next boot.
        request.cancel()
        with suppress(asyncio.CancelledError, RuntimeError):
            await request


async def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    state = root / "camera-state"
    factory = provision(root / "factory", args.controller)
    code = json.loads((factory.parent / "onboarding.json").read_bytes())["qrCode"]
    fabric = 32708
    with socket.socket(type=socket.SOCK_DGRAM) as probe:
        probe.bind((args.ip, 5540))
    with ExitStack() as services:
        gateway, _, _, rtsp = start_media(services, root, args)
        publishers = publish_av(services, root, args, rtsp, gateway.publisher)

        # Each boot uses only the disk state the previous device process left.
        # Keep the generated source live while rebooting both operational nodes
        # and the administrator, so there are no cached CASE connections.
        def boot(stack, folder, initial=False):
            folder.mkdir(mode=0o700)
            flags = ["--factory-data", factory]
            if initial:
                flags.append("--open-commissioning-window")
            device = start_device(stack, folder, args, gateway, rtsp, state,
                                  initialize=initial, extra_args=flags)
            wait_path(gateway, lambda p: p.get("ready") and len(p["readers"]) == 1, (*publishers, device))
            return device

        async def write(admin, entries):
            result = await admin.request("write_attribute", node_id=camera, attribute_path="0/31/0", value=entries)
            assert len(result) == 1 and result[0]["Status"] == 0
            assert result[0]["Path"] == {"EndpointId": 0, "ClusterId": 31, "AttributeId": 0}

        initial = root / "initial"
        with ExitStack() as stack:
            boot(stack, initial, True)
            a, v = ports(2)
            async with controller(stack, initial, args, "admin", a, fabric) as admin, viewer(initial, args, v) as call:
                async def commission(ticket):
                    reply = await admin.request("commission_with_code", code=ticket, network_only=True,
                                                discovery_timeout_s=60)
                    return reply["node_id"]

                camera = await commission(code)
                other = await commission((await call())["code"])
                identity = await call("identity")
                assert int(identity["node"]) == other and int(identity["fabric"]) == fabric
                original = await attribute(admin, camera, "0/31/0")
                assert len(original) == 1 and original[0]["1"] == 5 and int(original[0]["3"][0]) != other
                grant = dict(original[0], **{"1": 4, "3": [other], "4": [{"0": 1363, "1": 1, "2": None}]})
                granted = [*original, grant]
                await write(admin, granted)
                assert await call("sessions", camera=camera) == {"sessions": 0}

        cases = [
            # (mutation, interruption point, expected access on this boot)
            ("revoke", "before-replace", True), ("revoke", "after-sync", True),
            ("grant", "before-replace", False), ("grant", "after-sync", False),
            ("revoke", "after-reply", True), ("inspect", "final", False),
        ]
        evidence = []
        for action, boundary, allowed in cases:
            folder = root / f"{action}-{boundary}"
            with ExitStack() as stack:
                device = boot(stack, folder)
                a, v = ports(2)
                async with controller(stack, folder, args, "admin", a, fabric,
                                      existing_storage=initial / "admin") as admin, viewer(
                    folder, args, v, existing_storage=initial / "viewer-state",
                ) as call:
                    await call()  # startup handshake; onboarding data stays in the private pipe
                    assert await call("identity") == identity
                    assert await attribute(admin, camera, "0/62/3") == 1
                    assert await attribute(admin, camera, "0/60/0") == 0
                    assert await attribute(admin, camera, "0/31/0") == (granted if allowed else original)
                    assert await attribute(admin, camera, "1/1363/0") == []
                    assert await call("sessions", camera=camera) == (
                        {"sessions": 0} if allowed else {"status": 0x7e}
                    )
                    assert not (state / "state.next").exists(), "restart retained a staging snapshot"
                    async with measured_peer(admin, camera, 1, "receiver-offer", 45) as (media, _, _):
                        assert media["distinct_images"] > 1
                    row = dict(action=action, boundary=boundary, viewer_permitted_on_boot=allowed,
                               administrator_decoded_frames=media["decoded_frames"])
                    if action != "inspect":
                        entries = granted if action == "grant" else original
                        if boundary == "after-reply":
                            await write(admin, entries)
                            device.kill()
                            assert await asyncio.to_thread(device.wait, 10) == -signal.SIGKILL
                        else:
                            await interrupt(stack, folder, args, device, boundary, "::set_acl",
                                            lambda: write(admin, entries))
                            staged = (state / "state.next").is_file()
                            assert staged == (boundary == "before-replace")
                            row["staging_file_at_exit"] = staged
                    evidence.append(row)
            wait_path(gateway, lambda p: p.get("ready") and not p["readers"], publishers)
        # The SDK stores the allocation cursor and fabric under distinct keys.
        # Interrupt removal of the actual fabric record after the cursor write.
        guest_fabric = fabric + 1
        enroll = root / "enroll-guest"
        with ExitStack() as stack:
            boot(stack, enroll)
            a, b = ports(2)
            async with controller(stack, enroll, args, "admin", a, fabric,
                                  existing_storage=initial / "admin") as admin, controller(
                stack, enroll, args, "guest", b, guest_fabric,
            ) as guest:
                invitation = await admin.request("open_commissioning_window", node_id=camera, timeout=180,
                                                 iteration=1000, option=1)
                reply = await guest.request("commission_on_network", setup_pin_code=invitation["setup_pin_code"],
                                            ip_addr=args.ip, discovery_timeout_s=60)
                guest_camera = reply["node_id"]
                primary_index = await attribute(admin, camera, "0/62/5")
                guest_index = await attribute(guest, guest_camera, "0/62/5")
                assert primary_index != guest_index
                assert await fabric_ids(admin, camera) == {fabric: primary_index, guest_fabric: guest_index}

        removal = []
        for boundary in ("before-replace", "after-sync", "final"):
            folder = root / ("fabric-" + boundary)
            with ExitStack() as stack:
                device = boot(stack, folder)
                a, b = ports(2)
                async with controller(stack, folder, args, "admin", a, fabric,
                                      existing_storage=initial / "admin") as admin, controller(
                    stack, folder, args, "guest", b, guest_fabric, existing_storage=enroll / "guest",
                ) as guest:
                    expected = {fabric: primary_index}
                    if boundary != "final":
                        expected[guest_fabric] = guest_index
                    assert await fabric_ids(admin, camera) == expected
                    assert await attribute(admin, camera, "0/62/3") == len(expected)
                    assert await attribute(admin, camera, "0/60/0") == 0
                    assert await attribute(admin, camera, "1/1363/0") == []
                    assert not (state / "state.next").exists()
                    async with measured_peer(admin, camera, 1, "receiver-offer", 45) as (media, _, _):
                        row = dict(boundary=boundary, fabrics_on_boot=len(expected),
                                   administrator_decoded_frames=media["decoded_frames"])
                        if boundary != "final":
                            async with measured_peer(guest, guest_camera, 1, "camera-offer", 45) as (other_media, _, _):
                                row["guest_decoded_frames"] = other_media["decoded_frames"]
                        else:
                            # A fresh controller process still owns the retired identity.
                            # Membership was independently read from the surviving admin.
                            try:
                                async with asyncio.timeout(20):
                                    await attribute(guest, guest_camera, "0/62/5")
                            except RuntimeError:
                                row["retired_read"] = "controller error"
                            except TimeoutError:
                                row["retired_read"] = "no response within 20 seconds"
                            else:
                                raise AssertionError("retired fabric still read the node")
                    if boundary != "final":
                        async def remove():
                            reply = await admin.request("device_command", node_id=camera, endpoint_id=0,
                                                        cluster_id=62, command_name="RemoveFabric",
                                                        payload={"fabricIndex": guest_index})
                            assert reply["statusCode"] == 0

                        await interrupt(stack, folder, args, device, boundary, "::handle_remove_fabric",
                                        remove, remove_key=guest_index)
                        assert (state / "state.next").is_file() == (boundary == "before-replace")
                    removal.append(row)
            wait_path(gateway, lambda p: p.get("ready") and not p["readers"], publishers)
        return dict(viewer_permission=evidence, fabric_removal=removal)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "controller", "device", "gateway", "publisher", "mediamtx"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--lldb", default="lldb")
    parser.add_argument("--interface", required=True)
    parser.add_argument("--ip", required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    (args.runtime / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
