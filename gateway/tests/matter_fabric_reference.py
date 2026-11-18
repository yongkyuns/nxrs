#!/usr/bin/env python3
"""Commission two isolated fabrics and remove one while both receive the same gateway media."""
import argparse
import asyncio
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager, suppress
import json
from pathlib import Path
import socket
import time

import aiohttp
from aiortc.mediastreams import MediaStreamError

from matter_gateway_reference import start, stop
from matter_reference import Controller, measured_peer, playback
from matter_rtsp_reference import ports, publish, start_device, start_media, wait_path


@asynccontextmanager
async def controller(stack, root, args, label, port, fabric, *, existing_storage=None):
    storage = existing_storage or root / label
    if existing_storage is None:
        storage.mkdir(mode=0o700)
    else:
        assert storage.is_dir(), "missing enrolled controller"
    process = start(stack, root, label, [args.controller, "--storage-path", storage,
        "--primary-interface", args.interface, "--port", str(port), "--listen-address", "127.0.0.1",
        "--fabricid", str(fabric), "--enable-test-net-dcl", "--dcl-offline", "--disable-ota",
        "--disable-thread-diagnostics", "--log-level", "warning"])
    async with aiohttp.ClientSession() as http:
        deadline = time.monotonic() + 12
        while True:
            try:
                ws = await http.ws_connect(f"http://127.0.0.1:{port}/ws", max_msg_size=4194304)
                break
            except aiohttp.ClientConnectionError:
                assert process.poll() is None and time.monotonic() < deadline, "controller startup"
                await asyncio.sleep(.1)
        async with ws:
            await ws.receive_json(timeout=10)
            c = Controller(ws)
            job = asyncio.create_task(c.read())
            try:
                nodes = await c.request("start_listening")
                if existing_storage is None:
                    assert not nodes
                yield c
            finally:
                job.cancel()
                with suppress(asyncio.CancelledError):
                    await job


async def attribute(c, node, path):
    data = await c.request("read_attribute", node_id=node, attribute_path=path)
    return data[path]


async def fabric_ids(controller, node):
    path = "0/62/1"  # OperationalCredentials.Fabrics, read by a commissioned administrator.
    result = await controller.request("read_attribute", node_id=node, attribute_path=path, fabric_filtered=False)
    return {int(item["3"]): int(item["254"]) for item in result[path]}


async def packets(peer):
    stats = await peer.getStats()
    return sum(getattr(item, "packetsReceived", 0) for item in stats.values() if item.type == "inbound-rtp")


class Frames:
    def __init__(self, size=(640, 360)):
        self.size = size
        self.count = 0
        self.last_pts = None

    async def consume(self, track):
        with suppress(MediaStreamError):
            while True:
                frame = await track.recv()
                assert (frame.width, frame.height) == self.size
                assert self.last_pts is None or frame.pts > self.last_pts, "decoded clock went backwards"
                self.count += 1
                self.last_pts = frame.pts


async def compare(a, aid, b, bid, b_index, gateway):
    ca, cb = Frames(), Frames()
    async with AsyncExitStack() as peers:
        ar, ap, at = await peers.enter_async_context(measured_peer(a, aid, 1, "receiver-offer", 30))
        br, bp, bt = await peers.enter_async_context(measured_peer(b, bid, 1, "camera-offer", 30, request_end=False))
        tasks = [asyncio.create_task(c.consume(t)) for c, t in ((ca, at), (cb, bt))]
        try:
            deadline = time.monotonic() + 7
            while min(ca.count, cb.count) < 12:
                assert time.monotonic() < deadline and not any(t.done() for t in tasks)
                await asyncio.sleep(.05)
            path = gateway.path()
            assert len(path["readers"]) == 1 and path["ready"]
            assert await attribute(a, aid, "1/1363/0") and await attribute(b, bid, "1/1363/0")
            reply = await a.request("device_command", node_id=aid, endpoint_id=0, cluster_id=62,
                                    command_name="RemoveFabric", payload={"fabricIndex":b_index})
            assert reply["statusCode"] == 0
            assert await attribute(a, aid, "0/62/3") == 1
            # Keep both local WebRTC peers open. B never sends EndSession. Allow
            # queued RTP to drain and measure wire progress and decoded frames.
            await asyncio.sleep(2)
            a0, b0 = ca.count, cb.count
            start_packets = await packets(bp)
            assert start_packets > 0, "missing inbound RTP counters for removed peer"
            await asyncio.sleep(4)
            delta_a, delta_b = ca.count - a0, cb.count - b0
            delta_packets = (await packets(bp)) - start_packets
            print(f"Post-removal interval: surviving frames={delta_a}, removed frames={delta_b}, removed RTP packets={delta_packets}", flush=True)
            assert delta_a >= 30 and delta_b == 0 and delta_packets == 0, "fabric removal did not isolate media"
            assert ap.connectionState == "connected" and len(await attribute(a, aid, "1/1363/0")) == 1
            after = gateway.path()
            assert after["source"]["id"] == path["source"]["id"] and after["readers"] == path["readers"]
            return dict(initial_frames=[ar["decoded_frames"],br["decoded_frames"]],
                        post_removal_seconds=4, surviving_frames=delta_a, removed_frames=delta_b,
                        removed_rtp_packets=delta_packets, same_upstream_reader=True)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    state = root / "device-state"
    # The pinned camera example listens on the standard Matter port.
    with socket.socket(type=socket.SOCK_DGRAM) as check:
        check.bind((args.ip, 5540))
    with ExitStack() as stack:
        gateway, _, _, rtsp = start_media(stack, root, args)
        publish(stack, root, args, rtsp, gateway.publisher, "red", "640x360", 15)
        device = start_device(stack, root, args, gateway, rtsp, state, initialize=True)
        wait_path(gateway, lambda p: p.get("ready") and len(p["readers"]) == 1, (device,))
        port_a, port_b, port_c = ports(3)
        async with controller(stack, root, args, "a", port_a, 32701) as a, controller(stack, root, args, "b", port_b, 32702) as b:
            async def commission(c, pin):
                reply = await c.request("commission_on_network", setup_pin_code=pin, ip_addr=args.ip,
                                        discovery_timeout_s=60)
                return reply["node_id"]
            aid = await commission(a, 20202021)  # Public SDK fixture commissioning code.
            assert await attribute(a, aid, "0/62/3") == 1
            window = await a.request("open_commissioning_window", node_id=aid, timeout=180, iteration=1000, option=1)
            assert window["commissioning_timeout"] == 180
            bid = await commission(b, window["setup_pin_code"])
            del window
            assert await attribute(a, aid, "0/62/3") == await attribute(b, bid, "0/62/3") == 2
            ai, bi = await attribute(a, aid, "0/62/5"), await attribute(b, bid, "0/62/5")
            assert ai != bi and min(ai, bi) > 0
            assert await fabric_ids(a, aid) == {32701: ai, 32702: bi}
            print("Two fabrics commissioned with distinct indices", flush=True)
            report = await compare(a, aid, b, bid, bi, gateway)
            assert not await attribute(a, aid, "1/1363/0")
            stop(device)
            wait_path(gateway, lambda p: p.get("ready") and not p["readers"])
            restart = root / "restart"
            restart.mkdir(mode=0o700)
            device = start_device(stack, restart, args, gateway, rtsp, state)
            wait_path(gateway, lambda p: p.get("ready") and len(p["readers"]) == 1, (device,))
            async with asyncio.timeout(35):
                while True:
                    try:
                        count = await attribute(a, aid, "0/62/3")
                        break
                    except RuntimeError:
                        await asyncio.sleep(.5)
            assert count == 1 and await fabric_ids(a, aid) == {32701: ai}
            assert await attribute(a, aid, "0/60/0") == 0, "unexpected commissioning window after restart"
            report["surviving_playback_after_restart"] = await playback(a, aid, 1, "receiver-offer", 30)
            # SDK allocation may wrap at 254. Each independent controller starts
            # from its own CA and first node ID. A removes it over the wire.
            for attempt in range(1, 255):
                with ExitStack() as transient:
                    async with controller(transient, root, args, f"c-{attempt}", port_c, 32703) as c:
                        window = await a.request("open_commissioning_window", node_id=aid, timeout=180, iteration=1000, option=1)
                        cid = await commission(c, window["setup_pin_code"])
                        del window
                        ci = await attribute(c, cid, "0/62/5")
                        assert await fabric_ids(a, aid) == {32701: ai, 32703: ci}
                        if ci != bi and args.exercise_index_wrap:
                            reply = await a.request("device_command", node_id=aid, endpoint_id=0, cluster_id=62,
                                                    command_name="RemoveFabric", payload={"fabricIndex": ci})
                            assert reply["statusCode"] == 0 and await fabric_ids(a, aid) == {32701: ai}
                            if attempt % 25 == 0:
                                print(f"Completed {attempt} commissioning cycles", flush=True)
                            continue
                        assert not await attribute(c, cid, "1/1363/0")
                        report["new_fabric_playback"] = await playback(c, cid, 1, "camera-offer", 30)
                        try:
                            async with asyncio.timeout(12):
                                await attribute(b, bid, "0/62/5")
                        except TimeoutError:
                            report["retired_controller_read"] = "no reply within 12 seconds"
                        except RuntimeError as error:
                            if "controller error " not in str(error):
                                raise
                            report["retired_controller_read"] = "controller rejected"
                        else:
                            raise AssertionError("retired controller could still read operational credentials")
                        assert await attribute(c, cid, "0/62/5") == ci
                        report["authorized_fabric_ids_and_indices_verified"] = True
                        report["commissioning_window_closed_after_restart"] = True
                        report["commissioning_cycles_after_restart"] = attempt
                        report["fabric_index_reused_with_empty_sessions"] = ci == bi
                        return report
            raise AssertionError("fabric index did not wrap through the removed index")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "gateway", "mediamtx", "publisher", "device", "controller"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--exercise-index-wrap", action="store_true", help="cycle real fabric commissioning until the retired index is reused")
    parser.add_argument("--interface", required=True)
    parser.add_argument("--ip", required=True, help="LAN address of this test host")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    (args.runtime / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print("Two-fabric camera trial passed")
