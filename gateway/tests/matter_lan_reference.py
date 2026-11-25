#!/usr/bin/env python3
"""Test a Matter bridge across a selected and excluded Linux veth network."""

import argparse
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import pwd
import socket
import struct
import subprocess
import sys
import time

import dns.message
import dns.name
import dns.rdatatype

from matter_commissioning_reference import provision
from matter_fabric_reference import attribute, controller
from matter_gateway_reference import start, stop
from matter_reference import measured_peer
from matter_rtsp_reference import ports, publish, start_media, wait_path
from matter_session_reference import selected_port
from matter_snapshot_reference import verify_jpeg
from matter_source_access import private_file

SELECTED = ("matter-selected", "nxrs-lan", "192.0.2.1", "fd3f:388:1::1", "192.0.2.2", "fd3f:388:1::2")
EXCLUDED = ("matter-excluded", "nxrs-other", "198.51.100.1", "fd3f:388:2::1", "198.51.100.2", "fd3f:388:2::2")
PEER_LINK = "lan0"


def ip(*args):
    subprocess.run(["sudo", "-n", "ip", *args], check=True, timeout=10)


def network(stack, addresses):
    name, link, host4, host6, peer4, peer6 = addresses
    ip("netns", "add", name)
    stack.callback(ip, "netns", "del", name)
    ip("link", "add", link, "type", "veth", "peer", "name", PEER_LINK, "netns", name)
    stack.callback(ip, "link", "del", link)
    ip("addr", "add", host4 + "/24", "dev", link)
    ip("-6", "addr", "add", host6 + "/64", "dev", link, "nodad")
    ip("link", "set", link, "up")
    ip("-n", name, "link", "set", "lo", "up")
    ip("-n", name, "addr", "add", peer4 + "/24", "dev", PEER_LINK)
    ip("-n", name, "-6", "addr", "add", peer6 + "/64", "dev", PEER_LINK, "nodad")
    ip("-n", name, "link", "set", PEER_LINK, "up")


def multicast_advertised(service, seconds=5):
    name = dns.name.from_text(service)
    query = dns.message.make_query(name, dns.rdatatype.PTR, id=0, flags=0).to_wire()
    group = "ff02::fb"
    iface = socket.if_nametoindex(PEER_LINK)
    membership = socket.inet_pton(socket.AF_INET6, group) + struct.pack("=I", iface)
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("::", 5353))
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP, membership)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, iface)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 255)
        sock.settimeout(0.4)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            sock.sendto(query, (group, 5353, 0, iface))
            try:
                packet = sock.recv(8192)
            except TimeoutError:
                continue
            response = dns.message.from_wire(packet)
            if any(row.name == name and row.rdtype == dns.rdatatype.PTR for row in response.answer):
                return True
    return False


def tcp_reachable(addr):
    try:
        with socket.create_connection((addr, 5540), timeout=1):
            return True
    except (ConnectionRefusedError, TimeoutError):
        return False


def udp_reachable(addr):
    # A zero-length UDP readiness check. Linux reports a closed local port
    # with ICMP; a listening Matter socket silently drops an empty datagram.
    domain = socket.AF_INET6 if ":" in addr else socket.AF_INET
    with socket.socket(domain, socket.SOCK_DGRAM) as sock:
        sock.settimeout(0.4)
        sock.connect((addr, 5540))
        sock.send(b"")
        try:
            sock.recv(32)
        except TimeoutError:
            return True
        except ConnectionRefusedError:
            return False
    raise AssertionError("unexpected reply to empty datagram")


async def play(args):
    args.interface = PEER_LINK
    root = args.runtime
    existing = root / "ipv6-controller" if args.peer == "recover" else None
    async with asyncio.timeout(85):
        with ExitStack() as stack:
            label = "recovered-controller" if existing else "ipv6-controller"
            async with controller(
                stack, root, args, label, ports(1)[0], 32727, existing_storage=existing
            ) as client:
                if existing:
                    (enrolled,) = await client.request("get_nodes")
                else:
                    ticket = json.loads((root / "factory/onboarding.json").read_bytes())
                    payload = await client.request("parse_onboarding_payload", code=ticket["qrCode"])
                    (identity,) = payload["payloads"]
                    enrolled = await client.request(
                        "commission_on_network",
                        setup_pin_code=identity["setup_pin_code"],
                        ip_addr=SELECTED[3],
                        discovery_timeout_s=60,
                    )
                return await verify_playback(client, enrolled["node_id"])


async def verify_playback(client, node):
    assert await attribute(client, node, "0/62/3") == 1
    assert await attribute(client, node, "0/60/0") == 0
    async with measured_peer(client, node, 1, "receiver-offer", 45) as (live, peer, _):
        selected_port(peer, SELECTED[2])
        image = await client.request("camera_snapshot", node_id=node, endpoint_id=1, codec="JPEG")
        connections = subprocess.check_output(
            ["ss", "-H", "-tn6", "state", "established", "dport", "=", ":5540"], text=True
        )
        assert connections.strip(), "Matter JPEG path did not establish IPv6 TCP"
        return dict(
            frames=live["decoded_frames"],
            snapshot_bytes=verify_jpeg(image, (640, 360), 0),
            matter_ipv6_tcp=True,
        )


def peer(args):
    if args.peer in ("play", "recover"):
        return asyncio.run(play(args))
    addresses = SELECTED if args.peer == "selected" else EXCLUDED
    response = {}
    for address in addresses[2:4]:
        suffix = "6" if ":" in address else "4"
        response["tcp" + suffix] = tcp_reachable(address)
        response["udp" + suffix] = udp_reachable(address)
    if args.peer == "excluded":
        response["cross_link_tcp4"] = tcp_reachable(SELECTED[2])
        response["cross_link_udp4"] = udp_reachable(SELECTED[2])
        response["cross_link_tcp6"] = tcp_reachable(SELECTED[3])
        response["cross_link_udp6"] = udp_reachable(SELECTED[3])
    service = "_matter._tcp.local." if args.operational else "_matterc._udp.local."
    response["ipv6_dns_sd"] = multicast_advertised(service)
    return response


def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    factory = provision(root / "factory", args.controller)
    user = pwd.getpwuid(os.getuid()).pw_name

    def observe(mode, *, operational=False):
        namespace = EXCLUDED[0] if mode == "excluded" else SELECTED[0]
        cmd = [
            "sudo",
            "-n",
            "ip",
            "netns",
            "exec",
            namespace,
            "runuser",
            "-u",
            user,
            "--",
            "/usr/bin/env",
            "PATH=" + os.environ["PATH"],
            sys.executable,
            Path(__file__).resolve(),
            "--peer",
            mode,
            "--runtime",
            root,
            "--controller",
            args.controller,
        ]
        if operational:
            cmd.append("--operational")
        result = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, timeout=100)
        return json.loads(result.stdout)

    with ExitStack() as resources:
        network(resources, SELECTED)
        network(resources, EXCLUDED)
        ip("-n", EXCLUDED[0], "route", "add", SELECTED[2] + "/32", "via", EXCLUDED[2])
        ip("-n", EXCLUDED[0], "-6", "route", "add", SELECTED[3] + "/128", "via", EXCLUDED[3])
        gateway, _, _, rtsp = start_media(resources, root, args)
        source = publish(resources, root, args, rtsp, gateway.publisher, "red", "640x360", 15)
        settings = dict(
            version=1,
            site_id="lab",
            camera_id="cam",
            ice_address=SELECTED[2],
            lan_interface=SELECTED[1],
            control=f"127.0.0.1:{gateway.control}",
            rtsp=f"127.0.0.1:{rtsp}",
            media_token_file=str(gateway.credentials["reader"]),
            detection_token_file=str(gateway.credentials["independent"]),
            factory_data=str(factory),
            state_directory=str(root / "state"),
            snapshot_ffmpeg=str(args.ffmpeg.resolve(strict=True)),
        )
        config = root / "matter.json"
        if args.expect_unscoped:
            del settings["lan_interface"]
        else:
            private_file(config, json.dumps(dict(settings, lan_interface=EXCLUDED[1])))
            rejected = start(
                resources,
                root,
                "wrong-interface",
                [args.device, "--config", config, "--initialize-state", "--open-commissioning-window"],
                dict(os.environ, RUST_LOG="error"),
            )
            assert rejected.wait(timeout=8) != 0 and not (root / "state").exists()
            config.unlink()
        private_file(config, json.dumps(settings))
        device = start(
            resources,
            root,
            "matter",
            [args.device, "--config", config, "--initialize-state", "--open-commissioning-window"],
            dict(os.environ, RUST_LOG="error", TMPDIR=str(root)),
        )
        wait_path(gateway, lambda p: p.get("ready") and len(p["readers"]) == 1, (device, *source))
        selected = observe("selected")
        excluded = observe("excluded")
        assert all(selected.values()), selected
        assert all(value == args.expect_unscoped for value in excluded.values()), excluded
        report = dict(selected=selected, excluded=excluded)
        if not args.expect_unscoped:
            report["rejected_wrong_interface"] = True
            report["playback"] = observe("play")
            report["operational_selected"] = observe("selected", operational=True)
            report["operational_excluded"] = observe("excluded", operational=True)
            assert all(report["operational_selected"].values()), report["operational_selected"]
            assert not any(report["operational_excluded"].values()), report["operational_excluded"]
            stop(device)
            del settings["lan_interface"]
            config.write_text(json.dumps(settings))
            device = start(
                resources,
                root,
                "restart",
                [args.device, "--config", config],
                dict(os.environ, RUST_LOG="error", TMPDIR=str(root)),
            )
            wait_path(gateway, lambda p: p.get("ready") and len(p["readers"]) == 1, (device, *source))
            report["inferred_interface_after_restart"] = observe("recover")
            assert not any(observe("excluded", operational=True).values())
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--controller", type=Path, required=True)
    for name in ("device", "gateway", "publisher", "mediamtx", "ffmpeg"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--peer", choices=("selected", "excluded", "play", "recover"))
    parser.add_argument("--operational", action="store_true")
    parser.add_argument("--expect-unscoped", action="store_true")
    arguments = parser.parse_args()
    if arguments.peer:
        print(json.dumps(peer(arguments)))
    else:
        if any(
            getattr(arguments, name) is None
            for name in ("device", "gateway", "publisher", "mediamtx", "ffmpeg")
        ):
            parser.error("the bridge and media fixture paths are required")
        result = run(arguments)
        (arguments.runtime / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
