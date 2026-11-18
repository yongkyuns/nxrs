#!/usr/bin/env python3
"""Exercise DNS-SD and configured SNTP on two private Linux veth networks."""

import argparse
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import pwd
import selectors
import socket
import struct
import subprocess
import sys
import time

import dns.message
import dns.rdatatype
import dns.rrset

from matter_attestation_reference import public_test_bundle
from matter_commissioning_reference import provision
from matter_fabric_reference import attribute, controller
from matter_gateway_reference import start, stop
from matter_lan_reference import EXCLUDED, PEER_LINK, SELECTED, ip, network
from matter_rtsp_reference import ports, publish, start_media, wait_path
from matter_source_access import private_file
from matter_time_reference import receive, time_command

SERVICE = "_ntp._udp.local."
INSTANCE = "Nxrs-clock." + SERVICE
HOST = "nxrs-clock.local."
DEFAULT_ADDRESS = "fd3f:388:1::3"
DHCPV6_ADDRESS = "fd3f:388:1::4"
CLOCKS = {
    "selected": (SELECTED[5], -3600, True),
    "excluded": (EXCLUDED[5], 3600, True),
    "fallback": (DEFAULT_ADDRESS, 1200, False),
    "dhcp": (SELECTED[4], -7200, False),
    "dhcpv6": (DHCPV6_ADDRESS, -10800, False),
}


def clock(args):
    address, offset, advertise = CLOCKS[args.clock]
    with ExitStack() as resources:
        poller = resources.enter_context(selectors.DefaultSelector())
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        ntp = resources.enter_context(socket.socket(family, socket.SOCK_DGRAM))
        ntp.bind((address, 123))
        poller.register(ntp, selectors.EVENT_READ)
        if advertise:
            mdns = resources.enter_context(socket.socket(socket.AF_INET6, socket.SOCK_DGRAM))
            mdns.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            mdns.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            mdns.bind(("::", 5353))
            group = socket.inet_pton(socket.AF_INET6, "ff02::fb")
            mdns.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP,
                            group + struct.pack("=I", socket.if_nametoindex(PEER_LINK)))
            poller.register(mdns, selectors.EVENT_READ)
        (args.runtime / (args.clock + ".ready")).touch()
        while True:
            for event, _ in poller.select():
                sender = event.fileobj
                data, client = sender.recvfrom(1232)
                if sender is ntp:
                    if len(data) != 48 or data[0] != 0x23:
                        continue
                    now = time.time() + offset + 2208988800
                    timestamp = struct.pack("!II", int(now) & 0xFFFFFFFF, int(now % 1 * 2**32))
                    response = bytes([0x24, 2]) + bytes(22) + data[40:48] + timestamp * 2
                    sender.sendto(response, client)
                    (args.runtime / (args.clock + ".used")).touch()
                else:
                    query = dns.message.from_wire(data)
                    if len(query.question) != 1:
                        continue
                    question = query.question[0]
                    if question.name.to_text().lower() != SERVICE or question.rdtype != dns.rdatatype.PTR:
                        continue
                    response = dns.message.make_response(query)
                    response.answer.append(dns.rrset.from_text(SERVICE, 60, "IN", "PTR", INSTANCE))
                    response.additional.append(dns.rrset.from_text(INSTANCE, 60, "IN", "SRV", "0 0 123 " + HOST))
                    response.additional.append(dns.rrset.from_text(HOST, 60, "IN", "AAAA", address))
                    sender.sendto(response.to_wire(), client)


async def peer(args):
    args.interface = PEER_LINK
    root = args.runtime
    storage = root / "viewer"
    with ExitStack() as services:
        async with controller(
            services, root, args, "viewer" if args.peer == "initialize" else args.peer, ports(1)[0], 32840,
            existing_storage=None if args.peer == "initialize" else storage,
        ) as client:
            if args.peer == "initialize":
                ticket = json.loads((root / "factory/onboarding.json").read_bytes())
                payload = await client.request("parse_onboarding_payload", code=ticket["qrCode"])
                (identity,) = payload["payloads"]
                bound = await client.request(
                    "commission_on_network", setup_pin_code=identity["setup_pin_code"],
                    ip_addr=SELECTED[3], discovery_timeout_s=45,
                )
                node = bound["node_id"]
                assert await attribute(client, node, "0/56/2") != 4
                await time_command(client, node, "SetDefaultNTP", defaultNtp=DEFAULT_ADDRESS)
            else:
                (bound,) = await client.request("get_nodes")
                node = bound["node_id"]
            assert await attribute(client, node, "0/56/4") == DEFAULT_ADDRESS
            async with asyncio.timeout(12):
                while await attribute(client, node, "0/56/2") != 4:
                    await asyncio.sleep(0.2)
                observed = int(await attribute(client, node, "0/56/0"))
            clock_name = {"discovered": "selected", "dhcp": "dhcp", "dhcpv6": "dhcpv6"}.get(
                args.peer, "fallback"
            )
            offset = CLOCKS[clock_name][1]
            expected = (int(time.time()) - 946684800 + offset) * 1_000_000
            assert abs(observed - expected) < 20_000_000
            return await receive(client, node)


def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    user = pwd.getpwuid(os.getuid()).pw_name
    factory = provision(root / "factory", args.controller)
    bundle = root / "attestation"
    public_test_bundle(args.sdk, bundle)

    def command(namespace, *tail, root_user=False):
        base = ["sudo", "-n", "ip", "netns", "exec", namespace]
        if not root_user:
            base.extend(["runuser", "-u", user, "--", "/usr/bin/env", "PATH=" + os.environ["PATH"]])
        return [*base, sys.executable, Path(__file__).resolve(), "--runtime", root, *tail]

    def observe(mode):
        output = subprocess.check_output(command(
            SELECTED[0], "--peer", mode, "--controller", args.controller,
        ), timeout=85)
        return json.loads(output)

    def enable_dhcp(services, *, dual_stack):
        target = "/run/systemd/network/05-nxrs-dhcp-time-test.network"
        assert not Path(target).exists()
        config = root / "dhcp-test.network"
        value = (
            f"[Match]\nName={SELECTED[1]}\n"
            f"[Network]\nDHCP={'yes' if dual_stack else 'ipv4'}\n"
            "KeepConfiguration=static\nIPv6AcceptRA=no\n"
            "[DHCPv4]\nUseDNS=no\nUseRoutes=no\nUseGateway=no\nUseNTP=yes\n"
        )
        expected = {("DHCPv4", socket.inet_aton(SELECTED[4]))}
        extra = []
        if dual_stack:
            value += "[DHCPv6]\nWithoutRA=solicit\nUseDNS=no\nUseNTP=yes\n"
            extra = [
                f"--dhcp-range={SELECTED[3]},{SELECTED[3]},64,120s",
                f"--dhcp-option=option6:ntp-server,[{DHCPV6_ADDRESS}]",
            ]
            expected.add(("DHCPv6", socket.inet_pton(socket.AF_INET6, DHCPV6_ADDRESS)))
        config.write_text(value)
        subprocess.run(["sudo", "-n", "install", "-d", "/run/systemd/network"], check=True)
        subprocess.run(["sudo", "-n", "install", "-m", "0644", config, target], check=True)
        services.callback(subprocess.run, ["sudo", "-n", "rm", "-f", target], check=True)
        server = start(services, root, "dhcp-server", [
            "sudo", "-n", "ip", "netns", "exec", SELECTED[0], "/usr/sbin/dnsmasq",
            "--keep-in-foreground", "--conf-file=/dev/null", "--port=0", "--bind-interfaces",
            "--interface=" + PEER_LINK, "--dhcp-authoritative", "--leasefile-ro", "--no-ping",
            f"--dhcp-range={SELECTED[2]},{SELECTED[2]},255.255.255.0,120s",
            "--dhcp-option=option:router", "--dhcp-option=option:ntp-server," + SELECTED[4],
            *extra,
        ])
        subprocess.run(["sudo", "-n", "networkctl", "reload"], check=True)
        subprocess.run(["sudo", "-n", "networkctl", "reconfigure", SELECTED[1]], check=True)
        deadline = time.monotonic() + 15
        while server.poll() is None and time.monotonic() < deadline:
            result = subprocess.run(["networkctl", "--json=short", "--no-pager", "status", SELECTED[1]],
                                    capture_output=True)
            if result.returncode == 0:
                servers = json.loads(result.stdout).get("NTP", [])
                actual = {(rec.get("ConfigSource"), bytes(rec.get("Address", []))) for rec in servers}
                if expected <= actual:
                    return
            time.sleep(0.2)
        raise AssertionError("selected test link did not acquire its DHCP NTP options")

    with ExitStack() as services:
        network(services, SELECTED)
        network(services, EXCLUDED)
        ip("-n", SELECTED[0], "-6", "addr", "add", DEFAULT_ADDRESS + "/64", "dev", PEER_LINK, "nodad")

        def start_clock(name, net):
            child = start(services, root, name, command(net, "--clock", name, root_user=True))
            deadline = time.monotonic() + 4
            while not (root / (name + ".ready")).exists():
                assert child.poll() is None and time.monotonic() < deadline
                time.sleep(0.05)
            return child

        start_clock("excluded", EXCLUDED[0])
        start_clock("fallback", SELECTED[0])
        gateway, media, _, rtsp = start_media(services, root, args)
        publishers = publish(services, root, args, rtsp, gateway.publisher, "red", "640x360", 15)
        config = root / "matter.json"
        private_file(config, json.dumps(dict(
            version=1, site_id="lab", camera_id="cam", lan_interface=SELECTED[1], ice_address=SELECTED[2],
            control=f"127.0.0.1:{gateway.control}", rtsp=f"127.0.0.1:{rtsp}",
            media_token_file=str(gateway.credentials["reader"]),
            detection_token_file=str(gateway.credentials["independent"]),
            factory_data=str(factory), state_directory=str(root / "state"), attestation_directory=str(bundle),
            snapshot_ffmpeg=str(args.ffmpeg.resolve(strict=True)), host_power_source="dc",
        )))

        def boot(name, *flags):
            device = start(services, root, name, [args.device, "--config", config, "--allow-test-attestation", *flags],
                           dict(os.environ, RUST_LOG="error", TMPDIR=str(root)))
            wait_path(gateway, lambda path: path.get("ready") and len(path["readers"]) == 1,
                      (device, media, *publishers))
            return device

        device = boot("initial", "--initialize-state", "--open-commissioning-window")
        before = observe("initialize")
        assert (root / "fallback.used").is_file()
        stop(device)
        selected = start_clock("selected", SELECTED[0])
        device = boot("discovered-boot")
        discovered = observe("discovered")
        assert (root / "selected.used").is_file()
        stop(device)
        stop(selected)
        device = boot("fallback-boot")
        fallback = observe("fallback-again")
        frames = [before, discovered, fallback]
        if args.dhcp:
            stop(device)
            (root / "selected.ready").unlink()
            start_clock("selected", SELECTED[0])
            start_clock("dhcp", SELECTED[0])
            with ExitStack() as dhcpv4:
                enable_dhcp(dhcpv4, dual_stack=False)
                device = boot("dhcp-boot")
                frames.append(observe("dhcp"))
                stop(device)
            assert (root / "dhcp.used").is_file()
            ip("-n", SELECTED[0], "-6", "addr", "add", DHCPV6_ADDRESS + "/64", "dev", PEER_LINK, "nodad")
            start_clock("dhcpv6", SELECTED[0])
            enable_dhcp(services, dual_stack=True)
            boot("dhcpv6-boot")
            frames.append(observe("dhcpv6"))
            assert (root / "dhcpv6.used").is_file()
        assert not (root / "excluded.used").exists()
    return dict(
        selected_lan_dns_sd=True, excluded_lan_clock_unused=True,
        preferred_discovered_clock=True, fallback_after_withdrawal=True,
        frames=frames, dhcpv4_preferred=args.dhcp, dhcpv6_preferred=args.dhcp,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "sdk", "controller", "device", "gateway", "publisher", "mediamtx", "ffmpeg"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--clock", choices=CLOCKS)
    parser.add_argument("--peer", choices=("initialize", "discovered", "fallback-again", "dhcp", "dhcpv6"))
    parser.add_argument("--dhcp", action="store_true")
    args = parser.parse_args()
    if args.clock:
        clock(args)
    elif args.peer:
        print(json.dumps(asyncio.run(peer(args))))
    else:
        result = run(args)
        (args.runtime / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
