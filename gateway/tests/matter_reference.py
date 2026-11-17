#!/usr/bin/env python3
"""Independent Matter Server + aiortc camera witness (no live gateway mutation).

Uses media-requirements.txt. The explicitly selected --test-commission-ip path
commissions the upstream rs-matter example with its PUBLIC test passcode. It is
not a production provisioning path. Keep controller/device state outside Git.
"""
import argparse
import asyncio
import contextlib
import hashlib
import json
import time

import aiohttp
from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpSender, RTCSessionDescription
from aiortc.sdp import SessionDescription, candidate_from_sdp, candidate_to_sdp


def separate_candidates(sdp):
    """Force candidates through Matter commands instead of inline SDP shortcuts."""
    description = SessionDescription.parse(sdp)
    candidates = []
    for index, media in enumerate(description.media):
        candidates.extend(dict(candidate="candidate:" + candidate_to_sdp(candidate),
                               sdpMid=media.rtp.muxId, sdpMLineIndex=index)
                          for candidate in media.ice_candidates)
    # Preserve all unrelated SDP exactly; a parser/serializer round trip may
    # drop attributes (for example rtcp-mux without a separate RTCP port).
    stripped = "".join(line for line in sdp.splitlines(keepends=True)
                       if not line.startswith(("a=candidate:", "a=end-of-candidates")))
    return stripped, candidates


class Controller:
    def __init__(self, ws):
        self.ws = ws
        self.pending = {}
        self.events = asyncio.Queue(maxsize=128)
        self.sequence = 0

    async def read(self):
        try:
            async for message in self.ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    raise RuntimeError("controller connection closed")
                data = json.loads(message.data)
                future = self.pending.get(data.get("message_id"))
                if future is not None and not future.done():
                    if "error_code" in data:
                        # Server details can contain SDP or commissioning credentials.
                        future.set_exception(RuntimeError(f"controller error {data['error_code']}"))
                    else:
                        future.set_result(data["result"])
                elif data.get("event") in ("webrtc_callback", "camera_session_ended"):
                    self.events.put_nowait(data)
            raise RuntimeError("controller connection closed")
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("controller reader stopped"))

    async def request(self, command, **args):
        self.sequence += 1
        key = str(self.sequence)
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            await self.ws.send_json(dict(message_id=key, command=command, args=args))
            return await asyncio.wait_for(future, 120)
        except RuntimeError as error:
            raise RuntimeError(f"{command}: {error}") from error
        finally:
            self.pending.pop(key, None)


async def playback(controller, node_id, endpoint_id, flow, frame_count):
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    receiver = peer.addTransceiver("video", direction="recvonly")
    receiver.setCodecPreferences([
        codec for codec in RTCRtpSender.getCapabilities("video").codecs
        if codec.mimeType.lower() == "video/h264"
    ])
    tasks = []
    decoded = asyncio.get_running_loop().create_future()
    session = None
    target = dict(node_id=node_id, endpoint_id=endpoint_id)
    client_session_key = "nxrs-reference"
    candidate_counts = dict(sent=0, received=0)

    async def send_candidates(candidates):
        assert candidates, "local ICE gathering produced no candidates"
        await controller.request("camera_provide_ice_candidates", **target,
                                 client_session_key=client_session_key, ice_candidates=candidates)
        candidate_counts["sent"] += len(candidates)

    async def receive(track):
        stamps, hashes, first, last, size = [], set(), None, None, None
        try:
            while len(stamps) < frame_count:
                frame = await track.recv()
                now = time.monotonic()
                if first is None:
                    first = now
                last, size = now, [frame.width, frame.height]
                stamps.append(frame.pts)
                assert frame.format.name in ("yuv420p", "yuvj420p"), "unexpected decoded pixel format"
                plane = frame.planes[0]
                pixels = memoryview(bytes(plane))
                digest = hashlib.sha256()
                for row in range(frame.height):
                    offset = row * plane.line_size
                    digest.update(pixels[offset:offset + frame.width])
                hashes.add(digest.digest())  # Exclude uninitialized row padding.
            assert len(hashes) > 1, "decoded images did not change"
            assert all(a < b for a, b in zip(stamps, stamps[1:])), "nonmonotonic image timestamps"
            decoded.set_result(dict(
                decoded_frames=len(stamps), distinct_images=len(hashes), dimensions=size,
                decoded_fps=round((len(stamps) - 1) / (last - first), 2),
                connection_state=peer.connectionState,
            ))
        except Exception as error:
            if not decoded.done():
                decoded.set_exception(error)

    @peer.on("track")
    def on_track(track):
        if track.kind == "video":
            tasks.append(asyncio.create_task(receive(track)))

    async def signalling():
        pending_candidates = []
        while True:
            event = await controller.events.get()
            value = event["data"]
            if value.get("node_id") != node_id or value.get("endpoint_id") != endpoint_id:
                continue
            if event["event"] == "camera_session_ended":
                raise RuntimeError("camera ended before decoded-frame witness completed")
            kind, body = value["event_type"], value["data"]
            if kind in ("offer", "answer"):
                remote_sdp, _ = separate_candidates(body["sdp"])
                await peer.setRemoteDescription(RTCSessionDescription(sdp=remote_sdp, type=kind))
                if kind == "offer":
                    await peer.setLocalDescription(await peer.createAnswer())
                    answer, candidates = separate_candidates(peer.localDescription.sdp)
                    await controller.request(
                        "camera_provide_answer", **target,
                        webrtc_session_id=value["webrtc_session_id"], sdp=answer,
                    )
                    await send_candidates(candidates)
            elif kind == "ice_candidates":
                pending_candidates.extend(body["ice_candidates"])
            elif kind == "end":
                raise RuntimeError("camera ended before decoded-frame witness completed")
            if peer.remoteDescription:
                for raw in pending_candidates:
                    candidate = candidate_from_sdp(raw["candidate"].removeprefix("candidate:"))
                    candidate.sdpMid = raw.get("sdpMid")
                    candidate.sdpMLineIndex = raw.get("sdpMLineIndex")
                    await peer.addIceCandidate(candidate)
                    candidate_counts["received"] += 1
                pending_candidates.clear()

    signal_task = asyncio.create_task(signalling())
    tasks.append(signal_task)
    try:
        args = dict(**target, stream_usage="LiveView", audio=False,
                    allow_eviction=False, client_session_key=client_session_key)
        if flow == "receiver-offer":
            await peer.setLocalDescription(await peer.createOffer())
            args["sdp"], candidates = separate_candidates(peer.localDescription.sdp)
        session = await controller.request("camera_start_stream", **args)
        if flow == "receiver-offer":
            await send_candidates(candidates)
        done, _ = await asyncio.wait([decoded, signal_task], timeout=45,
                                     return_when=asyncio.FIRST_COMPLETED)
        if signal_task in done:
            signal_task.result()
        if decoded not in done:
            stats = await peer.getStats()
            packets = sum(getattr(item, "packetsReceived", 0) for item in stats.values()
                          if item.type == "inbound-rtp")
            raise TimeoutError(
                f"no complete decoded-frame witness: connection={peer.connectionState}, "
                f"ice={peer.iceConnectionState}, received_packets={packets}"
            )
        result = decoded.result()
        assert all(candidate_counts.values()), "bidirectional Matter ICE exchange was not witnessed"
        result.update(flow=flow, negotiated_codec=session["video"]["codec"],
                      trickle_ice=candidate_counts)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not decoded.done():
            decoded.cancel()
        await peer.close()
        # Also cancels a partially established session if start failed.
        ended = await controller.request("camera_stop_stream", **target,
                                         client_session_key=client_session_key)
        if session is not None and not ended["ended"]:
            raise RuntimeError("Matter session teardown was not acknowledged")
    result["teardown_acknowledged"] = True
    return result


async def run(args):
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(args.server, max_msg_size=4 * 1024 * 1024) as ws:
            hello = await ws.receive_json()
            controller = Controller(ws)
            reader = asyncio.create_task(controller.read())
            try:
                nodes = await controller.request("start_listening")
                if args.test_commission_ip:
                    assert not nodes, "refusing test commissioning into a nonempty controller"
                    node = await controller.request(
                        "commission_on_network", setup_pin_code=20202021,
                        ip_addr=args.test_commission_ip, discovery_timeout_s=60,
                    )
                    nodes = [node]
                if args.node_id is None:
                    assert len(nodes) == 1, "select --node-id when the controller has multiple nodes"
                    node_id = nodes[0]["node_id"]
                else:
                    node_id = args.node_id
                capabilities = await controller.request(
                    "camera_get_capabilities", node_id=node_id, endpoint_id=args.endpoint_id,
                )
                result = dict(controller=hello.get("sdk_version"),
                              schema_version=hello.get("schema_version"),
                              camera_capabilities_read=bool(capabilities))
                if args.flow != "inventory":
                    result.update(await playback(controller, node_id, args.endpoint_id,
                                                 args.flow, args.frames))
                print(json.dumps(result, sort_keys=True))
            finally:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:15580/ws")
    parser.add_argument("--test-commission-ip")
    parser.add_argument("--node-id", type=int)
    parser.add_argument("--endpoint-id", type=int, default=1)
    parser.add_argument("--flow", choices=("inventory", "receiver-offer", "camera-offer"),
                        default="receiver-offer")
    parser.add_argument("--frames", type=int, default=60)
    args = parser.parse_args()
    if not 30 <= args.frames <= 1800:
        parser.error("--frames must be between 30 and 1800")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
