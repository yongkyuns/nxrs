#!/usr/bin/env python3
"""Qualify a bridge clock synchronized from a separately commissioned Matter node."""

import argparse
import asyncio
from contextlib import ExitStack
from hashlib import sha256
from ipaddress import IPv6Address
import json
import os
from pathlib import Path
import signal
import struct
import time

from matter_attestation_reference import public_test_bundle
from matter_commissioning_reference import provision
from matter_fabric_reference import attribute, controller
from matter_gateway_reference import start, stop
from matter_reference import playback
from matter_rtsp_reference import ports, publish, start_media, wait_path
from matter_source_access import private_file


TIME = 56
TRUSTED_SOURCE_RECORD = 262  # Pinned rs-matter durable key.
DEFAULT_NTP_RECORD = 0x1006


async def time_command(client, node, command, **fields):
    result = await client.request(
        "device_command", node_id=node, endpoint_id=0, cluster_id=TIME, command_name=command, payload=fields,
    )
    assert result is None


async def wait_clock(client, node, sample):
    async with asyncio.timeout(18):
        while await attribute(client, node, "0/56/2") != 3:  # NodeTimeCluster
            await asyncio.sleep(0.15)
        utc = int(await attribute(client, node, "0/56/0"))
        assert sample <= utc < sample + 30_000_000
        assert await attribute(client, node, "0/56/1") == 2  # Seconds, after rounding down.
        local = int(await attribute(client, node, "0/56/7"))
        after = int(await attribute(client, node, "0/56/0"))
        assert utc <= local - 7200 * 1_000_000 <= after


async def wait_ntp(client, node):
    async with asyncio.timeout(12):
        while await attribute(client, node, "0/56/2") != 4:  # NonMatterSNTP
            await asyncio.sleep(0.15)
        sample = int(await attribute(client, node, "0/56/0"))
        expected = (int(time.time()) - 946684800) * 1_000_000
        assert abs(sample - expected) < 20_000_000


async def receive(client, node):
    async with asyncio.timeout(20):
        while True:
            try:
                result = await playback(client, node, 1, "receiver-offer", 30)
                assert result["dimensions"] == [640, 360]
                return result["decoded_frames"]
            except RuntimeError as error:
                if "(Matter status 203)" not in str(error):
                    raise
                await asyncio.sleep(0.15)


def records(snapshot):
    assert snapshot[:8] == b"NXMAT\0\0\x01" and sha256(snapshot[:-32]).digest() == snapshot[-32:]
    entries = {}
    offset = 18
    for _ in range(struct.unpack_from("<H", snapshot, 16)[0]):
        key, size = struct.unpack_from("<HI", snapshot, offset)
        end = offset + 6 + size
        entries[key] = snapshot[offset:end]
        offset = end
    assert offset == len(snapshot) - 32
    return entries


async def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    bundle = root / "attestation"
    public_test_bundle(args.sdk, bundle)
    factory = provision(root / "factory", args.controller)
    camera_invitation = json.loads((factory.parent / "onboarding.json").read_bytes())
    state = root / "state"
    snapshot = state / "state.bin"

    with ExitStack() as services:
        gateway, media, _, rtsp = start_media(services, root, args)
        encoder, publisher = publish(services, root, args, rtsp, gateway.publisher, "red", "640x360", 15)
        config = root / "matter.json"
        private_file(config, json.dumps(dict(
            version=1, site_id="lab", camera_id="cam", lan_interface=args.interface, ice_address=args.ip,
            control=f"127.0.0.1:{gateway.control}", rtsp=f"127.0.0.1:{rtsp}",
            media_token_file=str(gateway.credentials["reader"]),
            detection_token_file=str(gateway.credentials["independent"]),
            factory_data=str(factory), state_directory=str(state), attestation_directory=str(bundle),
            snapshot_ffmpeg=str(args.ffmpeg.resolve(strict=True)), host_power_source="dc",
        )))

        async def boot(label, *flags):
            device = start(
                services, root, label, [args.device, "--config", config, "--allow-test-attestation", *flags],
                dict(os.environ, RUST_LOG="error,rs_matter_dm::clusters::time_sync::client=info", TMPDIR=str(root)),
            )
            await asyncio.to_thread(
                wait_path, gateway, lambda path: path.get("ready") and len(path["readers"]) == 1,
                (device, media, encoder, publisher),
            )
            return device

        device = await boot("initial", "--initialize-state", "--open-commissioning-window")
        primary_port, secondary_port, source_port = ports(3)
        onboarding = root / "clock-onboarding.json"
        source = start(services, root, "clock", [
            "node", Path(__file__).with_name("matter") / "time-source.cjs",
            args.controller.absolute().parent.parent.parent, root / "clock-state",
            args.interface, str(source_port), onboarding,
        ])
        async with asyncio.timeout(12):
            while not onboarding.is_file():
                assert source.poll() is None, "independent clock exited"
                await asyncio.sleep(0.05)

        admin_process = []
        async with (
            controller(
                services, root, args, "admin", primary_port, 32830, process_started=admin_process.append
            ) as admin,
            controller(services, root, args, "owner", secondary_port, 32831) as owner,
        ):
            async def commission(client, code):
                result = await client.request(
                    "commission_with_code", code=code,
                    network_only=True, discovery_timeout_s=60,
                )
                return result["node_id"]

            camera = await commission(admin, camera_invitation["qrCode"])
            assert await attribute(admin, camera, "0/29/2") == [TIME]
            feature_map = 11 if args.default_ntp else 9
            assert await attribute(admin, camera, "0/56/65532") == feature_map
            assert await attribute(admin, camera, "0/56/3") is None
            if args.default_ntp:
                assert await attribute(admin, camera, "0/56/4") is None
                assert await attribute(admin, camera, "0/56/12") is False
                for invalid in ("clock.example", "fe80:1", "[::1]:123"):
                    try:
                        await time_command(admin, camera, "SetDefaultNTP", defaultNtp=invalid)
                    except RuntimeError as error:
                        assert "(Matter status 133)" in str(error), error  # InvalidCommand
                    else:
                        raise AssertionError("invalid NTP address accepted")
                assert await attribute(admin, camera, "0/56/4") is None
                await time_command(admin, camera, "SetDefaultNTP", defaultNtp=args.default_ntp)
                assert await attribute(admin, camera, "0/56/4") == args.default_ntp
                await wait_ntp(admin, camera)

            ticket = await admin.request(
                "open_commissioning_window", node_id=camera, timeout=180, iteration=1000, option=1,
            )
            owned = (await owner.request(
                "commission_on_network", setup_pin_code=ticket["setup_pin_code"], ip_addr=args.ip,
                discovery_timeout_s=60,
            ))["node_id"]
            clock = await commission(owner, json.loads(onboarding.read_bytes())["code"])
            assert clock != owned
            owner_index = await attribute(owner, owned, "0/62/5")
            acl = await attribute(owner, clock, "0/31/0")
            assert len(acl) == 1 and acl[0]["1"] == 5
            grant = {
                "1": 1, "2": 2, "3": [owned], "4": [{"0": TIME, "1": 0, "2": None}], "254": acl[0]["254"],
            }
            await owner.request("write_attribute", node_id=clock, attribute_path="0/31/0", value=[*acl, grant])
            assert await attribute(owner, clock, "0/31/0") == [*acl, grant]

            def trust():
                return time_command(owner, owned, "SetTrustedTimeSource", trustedTimeSource={"nodeId": clock, "endpoint": 0})

            # Keep the independent source distinguishable from the test host clock.
            instant = (int(time.time()) - 946684800 + 3600) * 1_000_000
            zone = await admin.request(
                "device_command", node_id=camera, endpoint_id=0, cluster_id=TIME, command_name="SetTimeZone",
                payload={"timeZone": [{"offset": 3600, "validAt": 0}]},
            )
            assert zone["dstOffsetRequired"]
            await time_command(
                admin, camera, "SetDSTOffset",
                dstOffset=[{"offset": 3600, "validStarting": 0, "validUntil": None}],
            )
            await time_command(owner, clock, "SetUTCTime", utcTime=instant, granularity=3)
            assert int(await attribute(owner, clock, "0/56/0")) == instant
            await trust()
            await wait_clock(admin, camera, instant)
            first_frames = await receive(admin, camera)
            await asyncio.to_thread(stop, device)
            retained = records(snapshot.read_bytes())[TRUSTED_SOURCE_RECORD]

            refreshed = instant + 120_000_000
            await time_command(owner, clock, "SetUTCTime", utcTime=refreshed, granularity=3)
            device = await boot("restarted")
            await wait_clock(admin, camera, refreshed)
            if args.default_ntp:
                assert await attribute(admin, camera, "0/56/4") == args.default_ntp
                # Keep the revocation test focused on a late trusted-peer sample.
                await time_command(admin, camera, "SetDefaultNTP", defaultNtp=None)
                assert await attribute(admin, camera, "0/56/4") is None

            # Hold the source while the bridge enters a refresh on the owning fabric.
            await time_command(owner, clock, "SetUTCTime", utcTime=refreshed + 120_000_000, granularity=3)
            refresh_marker = b"refreshing from fabric"
            log = root / "restarted.log"
            prior = log.read_bytes().count(refresh_marker)
            os.kill(source.pid, signal.SIGSTOP)
            try:
                await trust()
                async with asyncio.timeout(6):
                    while log.read_bytes().count(refresh_marker) == prior:
                        await asyncio.sleep(0.05)
                removed = await admin.request(
                    "device_command", node_id=camera, endpoint_id=0, cluster_id=62, command_name="RemoveFabric",
                    payload={"fabricIndex": owner_index},
                )
                assert removed["statusCode"] == 0
            finally:
                os.kill(source.pid, signal.SIGCONT)
            assert await attribute(admin, camera, "0/56/3") is None
            await asyncio.sleep(2)
            await wait_clock(admin, camera, refreshed)
            if args.default_ntp:
                await time_command(admin, camera, "SetDefaultNTP", defaultNtp=args.default_ntp)
                await wait_ntp(admin, camera)
            await asyncio.to_thread(stop, device)

        await asyncio.to_thread(stop, admin_process[0])
        # Model a crash after deleting the fabric and before deleting its source record.
        clean = snapshot.read_bytes()
        entries = records(clean)
        assert TRUSTED_SOURCE_RECORD not in entries
        entries[TRUSTED_SOURCE_RECORD] = retained
        interrupted = clean[:16] + struct.pack("<H", len(entries)) + b"".join(entries[k] for k in sorted(entries))
        snapshot.write_bytes(interrupted + sha256(interrupted).digest())
        await boot("interrupted-removal")
        async with controller(
            services, root, args, "restored-admin", ports(1)[0], 32830, existing_storage=root / "admin",
        ) as restored:
            assert await attribute(restored, camera, "0/62/3") == 1
            assert await attribute(restored, camera, "0/56/3") is None
            assert TRUSTED_SOURCE_RECORD not in records(snapshot.read_bytes())
            if args.default_ntp:
                assert await attribute(restored, camera, "0/56/4") == args.default_ntp
                await wait_ntp(restored, camera)
                await time_command(restored, camera, "SetDefaultNTP", defaultNtp=None)
                assert await attribute(restored, camera, "0/56/4") is None
                assert DEFAULT_NTP_RECORD not in records(snapshot.read_bytes())
            else:
                assert await attribute(restored, camera, "0/56/2") != 3
            last_frames = await receive(restored, camera)

    result = dict(
        case_authenticated_clock=True, picked_fresh_sample_after_restart=True,
        removed_trust_during_refresh=True, kept_previous_clock_on_revocation=True,
        repaired_interrupted_fabric_removal=True, verified_local_time=True, frames=[first_frames, last_frames],
    )
    if args.default_ntp:
        result.update(
            fallback_sntp_applied=True, trusted_matter_clock_preferred=True,
            fallback_restored_after_trust_removal=True, fallback_configuration_persisted_and_cleared=True,
            malformed_and_unsupported_addresses_rejected=True,
        )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "sdk", "controller", "device", "gateway", "publisher", "mediamtx", "ffmpeg"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--ip", required=True)
    parser.add_argument("--default-ntp", type=lambda value: str(IPv6Address(value)))
    args = parser.parse_args()
    result = asyncio.run(run(args))
    (args.runtime / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
