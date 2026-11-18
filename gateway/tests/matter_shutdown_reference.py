#!/usr/bin/env python3
"""Exercise negotiation deadlines and joined bridge shutdown through Matter."""

import argparse
import asyncio
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import signal
import socket
import time

from matter_commissioning_reference import provision
from matter_fabric_reference import Frames, attribute, controller, fabric_ids
from matter_gateway_reference import start
from matter_publication_reference import receiver_offer
from matter_reference import measured_peer, playback
from matter_rtsp_reference import ports, publish, start_media, wait_path
from matter_session_reference import selected_port, socket_owned
from matter_snapshot_reference import encoder_probe
from matter_source_access import private_file

SESSIONS = "1/1363/0"
ALLOCATIONS = "1/1361/15"
SNAPSHOTS = "1/1361/17"


@contextmanager
def suspend(process):
    process.send_signal(signal.SIGSTOP)
    try:
        yield
    finally:
        process.send_signal(signal.SIGCONT)


async def uncompleted_offer(client, node, key, kind, **fields):
    await client.request(
        "camera_start_stream",
        node_id=node,
        endpoint_id=1,
        stream_usage="LiveView",
        audio=False,
        allow_eviction=False,
        client_session_key=key,
        **fields,
    )
    session = candidate_port = None
    async with asyncio.timeout(8):
        while session is None or candidate_port is None:
            value = (await client.events.get())["data"]
            if value.get("client_session_key") != key:
                continue
            if value.get("event_type") == kind:
                session = value["webrtc_session_id"]
            elif value.get("event_type") == "ice_candidates":
                (candidate,) = value["data"]["ice_candidates"]
                candidate_port = int(candidate["candidate"].split()[5])
    return session, candidate_port


async def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    with socket.socket(type=socket.SOCK_DGRAM) as probe:
        probe.bind((args.ip, 5540))
    factory = provision(root / "factory", args.controller)
    code = json.loads((factory.parent / "onboarding.json").read_bytes())["qrCode"]
    encoder, delay, pid_file = encoder_probe(root, args.ffmpeg.resolve(strict=True))
    with ExitStack() as fixtures:
        gateway, _, _, rtsp = start_media(fixtures, root, args)
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
                    state_directory=str(root / "state"),
                    snapshot_ffmpeg=str(encoder),
                )
            ),
        )

        def launch(label, *flags):
            return start(
                fixtures,
                root,
                label,
                [args.device, "--config", config, *flags],
                dict(
                    os.environ,
                    RUST_LOG="error,rs_matter_dm::clusters::app::webrtc_prov=warn",
                    TMPDIR=str(root),
                ),
            )

        device = launch("matter", "--initialize-state", "--open-commissioning-window")
        publisher = publish(fixtures, root, args, rtsp, gateway.publisher, "red", "640x360", 15)

        def active_reader(path):
            return path.get("ready") and len(path["readers"]) == 1

        wait_path(gateway, active_reader, (device, *publisher))
        admin_port, guest_port = ports(2)
        guest_processes = []
        async with (
            controller(fixtures, root, args, "admin", admin_port, 32714) as client,
            controller(
                fixtures, root, args, "guest", guest_port, 32715, process_started=guest_processes.append
            ) as guest,
        ):
            (guest_process,) = guest_processes
            enrollment = await client.request(
                "commission_with_code", code=code, network_only=True, discovery_timeout_s=60
            )
            node = enrollment["node_id"]
            window = await client.request(
                "open_commissioning_window", node_id=node, timeout=180, iteration=1000, option=1
            )
            other = (
                await guest.request(
                    "commission_on_network",
                    setup_pin_code=window["setup_pin_code"],
                    ip_addr=args.ip,
                    discovery_timeout_s=60,
                )
            )["node_id"]
            original_fabrics = await fabric_ids(client, node)
            assert len(original_fabrics) == 2
            original_refs = sum(row["12"] for row in await attribute(client, node, ALLOCATIONS))

            async def references():
                return sum(row["12"] for row in await attribute(client, node, ALLOCATIONS))

            async def sessions(c=client, target=node, *, filtered=True):
                result = await c.request(
                    "read_attribute", node_id=target, attribute_path=SESSIONS, fabric_filtered=filtered
                )
                return {row["0"] for row in result[SESSIONS]}

            initial = await playback(client, node, 1, "camera-offer", 30)
            sdp = await receiver_offer()
            start_time = time.monotonic()
            waiting = dict(
                [
                    await uncompleted_offer(guest, other, "no-answer", "offer"),
                    await uncompleted_offer(guest, other, "no-ice", "answer", sdp=sdp),
                ]
            )
            assert await sessions(guest, other) == waiting.keys() and await references() == original_refs + 2
            assert all(socket_owned(args.ip, port) for port in waiting.values())
            async with measured_peer(client, node, 1, "receiver-offer", 45, request_end=False) as (
                live,
                peer,
                track,
            ):
                [live_id] = await sessions()
                live_port = selected_port(peer, args.ip)
                frames = Frames()
                drain = asyncio.create_task(frames.consume(track))
                try:
                    # The offline fabric cannot acknowledge either outbound End.
                    with suspend(guest_process):
                        async with asyncio.timeout(23):
                            while any(socket_owned(args.ip, port) for port in waiting.values()):
                                await asyncio.sleep(0.1)
                        timeout_seconds = time.monotonic() - start_time
                        assert 18 <= timeout_seconds <= 23, timeout_seconds
                        assert await references() == original_refs + 1
                        async with asyncio.timeout(5):
                            while await sessions(filtered=False) != {live_id}:
                                await asyncio.sleep(0.1)
                        retired_rows_seconds = time.monotonic() - start_time
                        assert retired_rows_seconds <= 25, retired_rows_seconds
                        previous = frames.count
                        async with asyncio.timeout(6):
                            while True:
                                assert not drain.done()
                                timed_out = (
                                    (root / "matter.log").read_text().count("outbound push failed: TxTimeout")
                                )
                                if timed_out >= 2 and frames.count >= previous + 15:
                                    break
                                await asyncio.sleep(0.025)
                    assert await sessions(guest, other) == set()

                    _, pending_port = await uncompleted_offer(guest, other, "shutdown", "offer")
                    assert socket_owned(args.ip, pending_port) and socket_owned(args.ip, live_port)
                    delay.touch()
                    snapshot = asyncio.create_task(
                        client.request("camera_snapshot", node_id=node, endpoint_id=1, codec="JPEG")
                    )
                    try:
                        async with asyncio.timeout(5):
                            while not pid_file.is_file():
                                if snapshot.done():
                                    snapshot.result()
                                    raise AssertionError("snapshot completed before delayed encoder started")
                                await asyncio.sleep(0.02)
                        pid = int(pid_file.read_text())
                        os.kill(pid, 0)
                        async with asyncio.timeout(3):
                            assert await sessions() == {live_id}
                            assert len(await sessions(guest, other)) == 1
                            assert len(await attribute(client, node, SNAPSHOTS)) == 1
                        before = time.monotonic()
                        device.terminate()
                        try:
                            await asyncio.wait_for(snapshot, 4)
                        except RuntimeError as error:
                            if str(error) != "camera_snapshot: controller error 0" and (
                                "(Matter status 203)" not in str(error)
                            ):
                                raise
                        else:
                            raise AssertionError("shutdown completed a delayed capture")
                        # The high-level controller may translate the Matter status.
                        # Prove its cleanup reached the bridge before transport exit.
                        assert device.poll() is None
                        async with asyncio.timeout(2):
                            assert await attribute(client, node, SNAPSHOTS) == []
                        assert await asyncio.to_thread(device.wait, 8) == 0
                        shutdown_seconds = time.monotonic() - before
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            pass
                        else:
                            raise AssertionError("encoder remained after Matter process exit")
                        assert not socket_owned(args.ip, live_port)
                        assert not socket_owned(args.ip, pending_port)
                    finally:
                        snapshot.cancel()
                        await asyncio.gather(snapshot, return_exceptions=True)
                finally:
                    drain.cancel()
                    await asyncio.gather(drain, return_exceptions=True)

            unplugged = await asyncio.to_thread(
                wait_path, gateway, lambda p: p.get("ready") and not p["readers"], publisher
            )
            publisher_id = unplugged["source"]["id"]
            restarted = launch("restart")
            healthy = await asyncio.to_thread(wait_path, gateway, active_reader, (restarted, *publisher))
            assert healthy["source"]["id"] == publisher_id
            async with asyncio.timeout(35):
                while True:
                    try:
                        assert await fabric_ids(client, node) == original_fabrics
                        break
                    except RuntimeError:
                        await asyncio.sleep(0.25)
            assert await sessions() == set() and await references() == original_refs
            assert await attribute(client, node, "0/60/0") == 0
            # Registration precedes the first validated H.264 keyframe.
            async with asyncio.timeout(8):
                while True:
                    try:
                        restored = await playback(client, node, 1, "camera-offer", 45)
                        break
                    except RuntimeError as error:
                        if "(Matter status 203)" not in str(error):
                            raise
                        await asyncio.sleep(0.15)
            return dict(
                baseline_frames=initial["decoded_frames"],
                timed_out_peers=len(waiting),
                timeout_seconds=round(timeout_seconds, 3),
                offline_fabric_rows_released_seconds=round(retired_rows_seconds, 3),
                outbound_delivery_timeouts=timed_out,
                negotiation_sockets_and_refs_released=True,
                live_frames=live["decoded_frames"],
                live_frames_after_peer_timeouts=15,
                matter_read_during_delayed_snapshot=True,
                shutdown_capture_rejected_and_released=True,
                graceful_exit_seconds=round(shutdown_seconds, 3),
                active_and_pending_sockets_closed=True,
                running_snapshot_encoder_reaped=True,
                same_publisher=True,
                enrollment_preserved=True,
                frames_after_restart=restored["decoded_frames"],
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "device", "controller", "gateway", "publisher", "mediamtx", "ffmpeg"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--ip", required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    (args.runtime / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
