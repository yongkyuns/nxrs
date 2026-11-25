#!/usr/bin/env python3
"""Qualify Matter video from a routed WireGuard viewer in a disposable Linux guest."""

import argparse
import asyncio
from contextlib import ExitStack, asynccontextmanager, suppress
import json
import os
from pathlib import Path
import pwd
import socket
import subprocess
import sys

import aiohttp

from matter_attestation_reference import public_test_bundle
from matter_commissioning_reference import provision
from matter_fabric_reference import attribute
from matter_gateway_reference import start, stop
from matter_lan_reference import PEER_LINK, SELECTED, ip, network
from matter_reference import Controller, measured_peer
from matter_rtsp_reference import ports, publish, start_media, wait_path
from matter_session_reference import selected_port
from matter_source_access import private_file

ROUTER, LAN_LINK, CAMERA, CAMERA_V6, SIGNALLING, _ = SELECTED
REMOTE = "matter-routed-viewer"
WIREGUARD = "wg0"
ROUTER_WAN, VIEWER_WAN = "198.51.100.1", "198.51.100.2"
ROUTER_VPN, VIEWER_VPN = "203.0.113.1", "203.0.113.2"


def namespace(name, *command):
    return ["sudo", "-n", "ip", "netns", "exec", name, *command]


def unprivileged(name, *command):
    user = pwd.getpwuid(os.getuid()).pw_name
    return namespace(name, "runuser", "-u", user, "--", "/usr/bin/env",
                     "PATH=" + os.environ["PATH"], *command)


def wireguard(stack):
    network(stack, SELECTED)
    ip("netns", "add", REMOTE)
    stack.callback(ip, "netns", "del", REMOTE)
    ip("-n", ROUTER, "link", "add", "wan0", "type", "veth", "peer", "name", "wan0", "netns", REMOTE)
    keys = [subprocess.check_output(["wg", "genkey"], timeout=3) for _ in range(2)]
    public = [subprocess.check_output(["wg", "pubkey"], input=key, timeout=3).strip().decode()
              for key in keys]
    ip("-n", REMOTE, "link", "set", "lo", "up")
    for i, (name, wan, vpn, allowed) in enumerate((
        (ROUTER, ROUTER_WAN, ROUTER_VPN, VIEWER_VPN + "/32"),
        (REMOTE, VIEWER_WAN, VIEWER_VPN, f"{CAMERA}/32,{SIGNALLING}/32"),
    )):
        ip("-n", name, "addr", "add", wan + "/24", "dev", "wan0")
        ip("-n", name, "link", "set", "wan0", "up")
        ip("-n", name, "link", "add", WIREGUARD, "type", "wireguard")
        ip("-n", name, "addr", "add", vpn + "/32", "dev", WIREGUARD)
        endpoint = ["endpoint", f"{ROUTER_WAN}:51820"] if name == REMOTE else []
        subprocess.run(namespace(name, "wg", "set", WIREGUARD, "listen-port", "51820",
                                 "private-key", "/dev/stdin", "peer", public[1-i],
                                 "allowed-ips", allowed, *endpoint),
                       input=keys[i], check=True, timeout=5)
        ip("-n", name, "link", "set", WIREGUARD, "up")
    for address in (CAMERA, SIGNALLING):
        ip("-n", REMOTE, "route", "add", address + "/32", "dev", WIREGUARD)
    ip("-n", ROUTER, "route", "add", VIEWER_VPN + "/32", "dev", WIREGUARD)
    subprocess.run(namespace(ROUTER, "sysctl", "-qw", "net.ipv4.ip_forward=1"), check=True, timeout=5)


def nft(rules):
    subprocess.run(namespace(ROUTER, "nft", "-f", "-"), input=rules,
                   text=True, check=True, timeout=5)


def media_packets():
    result = subprocess.check_output(namespace(ROUTER, "nft", "-j", "list", "chain",
                                               "inet", "nxrs_routed", "media"), timeout=5)
    rules = json.loads(result)["nftables"]
    return sum(expr["counter"]["packets"] for rule in rules if "rule" in rule
               for expr in rule["rule"]["expr"] if "counter" in expr)


@asynccontextmanager
async def connect(port, process=None):
    async with aiohttp.ClientSession() as http:
        async with asyncio.timeout(12):
            while True:
                try:
                    ws = await http.ws_connect(f"http://{SIGNALLING}:{port}/ws", max_msg_size=4194304)
                    break
                except aiohttp.ClientConnectionError:
                    if process is None or process.poll() is not None:
                        raise
                    await asyncio.sleep(0.1)
            await ws.receive_json()
        async with ws:
            controller = Controller(ws)
            task = asyncio.create_task(controller.read())
            try:
                yield controller, await controller.request("start_listening")
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


async def viewer(port, blocked):
    async with connect(port) as (client, nodes):
        (device,) = nodes
        node = device["node_id"]
        assert await client.request("camera_get_capabilities", node_id=node, endpoint_id=1)
        if blocked:
            expired = False
            try:
                async with asyncio.timeout(10):
                    async with measured_peer(client, node, 1, "receiver-offer", 30):
                        raise AssertionError("decoded video with the media route blocked")
            except TimeoutError:
                expired = True
            assert expired
            assert await attribute(client, node, "1/1363/0") == []
            return {"signalling_with_media_blocked": True, "blocked_session_retired": True}

        with socket.socket() as probe:
            probe.settimeout(0.6)
            assert probe.connect_ex((CAMERA, 5540)) != 0, "VPN viewer reached the direct Matter TCP port"
        report = {}
        for flow in ("receiver-offer", "camera-offer"):
            async with measured_peer(client, node, 1, flow, 45) as (result, peer, _):
                selected_port(peer, CAMERA)
                report[flow] = result["decoded_frames"]
        assert await attribute(client, node, "1/1363/0") == []
        return {"decoded_frames": report, "direct_matter_tcp_blocked": True, "sessions_retired": True}


async def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    factory = provision(root / "factory", args.controller)
    bundle = root / "attestation"
    public_test_bundle(args.sdk, bundle)
    port, = ports(1)
    assert port not in (5353, 5540, 51820)
    with ExitStack() as stack:
        wireguard(stack)
        nft(f'''
table inet nxrs_routed {{
  chain input {{
    type filter hook input priority filter; policy accept;
    iifname "{WIREGUARD}" ip daddr {SIGNALLING} tcp dport {port} accept
    iifname "{WIREGUARD}" drop
  }}
  chain forward {{
    type filter hook forward priority filter; policy drop;
    iifname "{PEER_LINK}" oifname "{WIREGUARD}" ct state established,related accept
    iifname "{WIREGUARD}" oifname "{PEER_LINK}" ip daddr {CAMERA} udp dport 1024-65535 udp dport != {{ 5353, 5540 }} jump media
  }}
  chain media {{ counter drop; }}
  chain source_nat {{
    type nat hook postrouting priority srcnat; policy accept;
    iifname "{WIREGUARD}" oifname "{PEER_LINK}" ip daddr {CAMERA} masquerade
  }}
}}''')
        gateway, _, _, rtsp = start_media(stack, root, args)
        source = publish(stack, root, args, rtsp, gateway.publisher, "red", "640x360", 15)
        settings = dict(version=1, site_id="lab", camera_id="cam", ice_address=CAMERA, lan_interface=LAN_LINK,
                        control=f"127.0.0.1:{gateway.control}", rtsp=f"127.0.0.1:{rtsp}",
                        media_token_file=str(gateway.credentials["reader"]),
                        detection_token_file=str(gateway.credentials["independent"]),
                        factory_data=str(factory), state_directory=str(root / "state"),
                        attestation_directory=str(bundle), snapshot_ffmpeg=str(args.ffmpeg.resolve(strict=True)),
                        host_power_source="dc")
        deployment = root / "matter.json"
        private_file(deployment, json.dumps(settings))
        device = start(stack, root, "matter", [args.device, "--config", deployment, "--allow-test-attestation",
                                               "--initialize-state", "--open-commissioning-window"],
                       dict(os.environ, TMPDIR=str(root), RUST_LOG="warn"))
        wait_path(gateway, lambda path: path.get("ready") and len(path["readers"]) == 1, (device, *source))
        storage = root / "lan-controller"
        storage.mkdir(mode=0o700)
        controller = start(stack, root, "controller", unprivileged(
            ROUTER, args.controller, "--listen-address", SIGNALLING, "--port", str(port),
            "--storage-path", storage, "--primary-interface", PEER_LINK, "--fabricid", "33388",
            "--enable-test-net-dcl", "--dcl-offline", "--disable-ota",
            "--disable-thread-diagnostics", "--log-level", "warning",
        ))
        async with connect(port, controller) as (client, nodes):
            assert not nodes
            onboarding = json.loads((factory.parent / "onboarding.json").read_bytes())
            payload = await client.request("parse_onboarding_payload", code=onboarding["qrCode"])
            (identity,) = payload["payloads"]
            await client.request("commission_on_network", setup_pin_code=identity["setup_pin_code"],
                                 ip_addr=CAMERA_V6, discovery_timeout_s=60)
        command = unprivileged(REMOTE, sys.executable, Path(__file__).resolve(), "--viewer", str(port))
        blocked = await asyncio.to_thread(subprocess.check_output, [*command, "--block-media"], timeout=30)
        dropped = media_packets()
        assert dropped > 0
        nft("flush chain inet nxrs_routed media\nadd rule inet nxrs_routed media counter accept\n")
        works = await asyncio.to_thread(subprocess.check_output, command, timeout=100)
        forwarded = media_packets()
        assert forwarded > 0
        transfer = subprocess.check_output(namespace(REMOTE, "wg", "show", WIREGUARD, "transfer"), timeout=5)
        _, received_bytes, sent_bytes = transfer.split()
        assert int(received_bytes) > 0 and int(sent_bytes) > 0, "WireGuard did not carry the trial"
        stop(controller)
        return {**json.loads(blocked), **json.loads(works), "dropped_udp_packets": dropped,
                "forwarded_udp_packets": forwarded, "wireguard_bidirectional": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "device", "sdk", "controller", "gateway", "publisher", "mediamtx", "ffmpeg"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--viewer", type=int)
    parser.add_argument("--block-media", action="store_true")
    args = parser.parse_args()
    if args.viewer is not None:
        result = asyncio.run(viewer(args.viewer, args.block_media))
    else:
        if any(getattr(args, name) is None for name in
               ("runtime", "device", "sdk", "controller", "gateway", "publisher", "mediamtx", "ffmpeg")):
            parser.error("all fixture paths are required")
        result = asyncio.run(run(args))
        (args.runtime / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
