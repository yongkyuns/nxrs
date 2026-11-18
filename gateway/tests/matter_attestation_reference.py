#!/usr/bin/env python3
"""Verify file-provisioned Matter identity, camera descriptors and fabric-scoped bindings."""

import argparse
import asyncio
from contextlib import ExitStack
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess

from matter_commissioning_reference import provision
from matter_fabric_reference import attribute, controller
from matter_gateway_reference import start, stop
from matter_reference import playback
from matter_rtsp_reference import ports, publish, start_media, wait_path
from matter_source_access import private_file


BINDING = "1/30/0"
WEBRTC_REQUESTOR = 0x0554


def binding_record(snapshot):
    # The isolated fixture uses the pinned SDK's Binding key in an Nxrs snapshot.
    assert snapshot[:8] == b"NXMAT\0\0\x01" and sha256(snapshot[:-32]).digest() == snapshot[-32:]
    offset = 18
    for _ in range(struct.unpack_from("<H", snapshot, 16)[0]):
        key, size = struct.unpack_from("<HI", snapshot, offset)
        end = offset + 6 + size
        if key == 260:
            return offset, end
        offset = end
    raise AssertionError("missing persisted Binding record")


async def write_binding(client, node, endpoint):
    index = await attribute(client, node, "0/62/5")
    acl_path = "0/31/0"
    result = await client.request("read_attribute", node_id=node, attribute_path=acl_path, fabric_filtered=True)
    acl = result[acl_path]
    assert len(acl) == 1 and acl[0]["1"] == 5 and acl[0]["254"] == index
    controller_node = int(acl[0]["3"][0])
    target = {"1": controller_node, "3": endpoint, "4": WEBRTC_REQUESTOR, "254": index}
    reply = await client.request("write_attribute", node_id=node, attribute_path=BINDING, value=[target])
    assert reply == [{"Path": {"EndpointId": 1, "ClusterId": 30, "AttributeId": 0}, "Status": 0}], reply
    return (controller_node, endpoint, WEBRTC_REQUESTOR, index)


async def read_bindings(client, node, *, filtered):
    result = await client.request("read_attribute", node_id=node, attribute_path=BINDING, fabric_filtered=filtered)
    return sorted(tuple(int(row[key]) for key in ("1", "3", "4", "254")) for row in result[BINDING])


def public_test_bundle(sdk, directory):
    expected = json.loads(Path(__file__).with_name("matter").joinpath("versions.json").read_bytes())
    git = ["git", "-C", str(sdk)]
    assert (
        subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
        == expected["rs_matter"]["revision"]
    )
    certs = subprocess.check_output([*git, "show", "HEAD:rs-matter-node-core/src/test_device.rs"], text=True)
    key_source = subprocess.check_output([*git, "show", "HEAD:rs-matter-crypto/src/lib.rs"], text=True)

    def array(source, name):
        literal = re.search(rf"\bconst {name}\s*:[^=]+=[^\[]*\[([^\]]+)\]", source)
        assert literal, f"missing pinned test vector {name}"
        return bytes(int(item, 16) for item in re.findall(r"0x[0-9a-fA-F]{2}\b", literal[1]))

    directory.mkdir(mode=0o700)
    for path, name in (
        ("dac.der", "DAC_CERT"),
        ("pai.der", "PAI_CERT"),
        ("certification-declaration.der", "CERT_DECLARATION"),
    ):
        private_file(directory / path, "")  # Create exclusively with the required private mode.
        (directory / path).write_bytes(array(certs, name))
    key = array(key_source, "TEST_DAC_PRIVKEY")
    assert len(key) == 32
    key_path = directory / "dac.key"
    private_file(key_path, "")
    key_path.write_bytes(key)
    identity = dict(
        version=1,
        vendor_id=0xFFF1,
        product_id=0x8001,
        vendor_name="Fixture Vendor",
        product_name="Provisioned Camera",
        serial_number="file-provisioned-1",
    )
    private_file(directory / "device.json", json.dumps(identity))
    return identity


async def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    with socket.socket(type=socket.SOCK_DGRAM) as probe:
        probe.bind((args.ip, 5540))
    bundle = root / "attestation"
    identity = public_test_bundle(args.sdk, bundle)
    identity_file = bundle / "device.json"
    key = bundle / "dac.key"
    declaration = bundle / "certification-declaration.der"
    factory = provision(root / "factory", args.controller)
    invitation = json.loads((factory.parent / "onboarding.json").read_bytes())
    state = root / "state"
    snapshot = state / "state.bin"

    with ExitStack() as services:
        gateway, server, _, rtsp = start_media(services, root, args)
        settings = dict(
            version=1,
            site_id="lab",
            camera_id="cam",
            lan_interface=args.interface,
            ice_address=args.ip,
            control=f"127.0.0.1:{gateway.control}",
            rtsp=f"127.0.0.1:{rtsp}",
            media_token_file=str(gateway.credentials["reader"]),
            detection_token_file=str(gateway.credentials["independent"]),
            factory_data=str(factory),
            state_directory=str(state),
            snapshot_ffmpeg=str(args.ffmpeg.resolve(strict=True)),
        )
        config = root / "matter.json"
        private_file(config, json.dumps(settings))

        def boot(label, *flags):
            return start(
                services,
                root,
                label,
                [args.device, "--config", config, *flags],
                dict(os.environ, RUST_LOG="error", TMPDIR=str(root)),
            )

        async def refuse(label, *flags):
            candidate = boot(label, *flags)
            assert await asyncio.to_thread(candidate.wait, 6) != 0, f"accepted {label}"

        init = ("--initialize-state", "--open-commissioning-window")
        dev_init = (*init, "--allow-test-attestation")
        await refuse("missing-bundle", *dev_init)
        settings["attestation_directory"] = str(bundle)
        config.write_text(json.dumps(settings))
        await refuse("development-chain-on-default-path", *init)

        key.chmod(0o644)
        await refuse("exposed-key", *dev_init)
        key.chmod(0o600)
        original_key = key.read_bytes()
        key.write_bytes(bytes([1]) * 32)
        await refuse("wrong-key", *dev_init)
        key.write_bytes(original_key)

        original_cd = declaration.read_bytes()
        declaration.write_bytes(original_cd[:-1] + bytes([original_cd[-1] ^ 1]))
        await refuse("altered-declaration", *dev_init)
        declaration.write_bytes(original_cd)
        identity_file.write_text(json.dumps(dict(identity, product_id=1)))
        await refuse("wrong-product", *dev_init)
        identity_file.write_text(json.dumps(identity))
        assert not state.exists()

        encoder, publisher = publish(services, root, args, rtsp, gateway.publisher, "red", "640x360", 15)
        device = boot("initialized", *dev_init)

        async def ready():
            await asyncio.to_thread(
                wait_path,
                gateway,
                lambda path: path.get("ready") and len(path["readers"]) == 1,
                (device, server, encoder, publisher),
            )

        await ready()
        admin_port, second_port = ports(2)
        admin_process = []
        async with (
            controller(
                services, root, args, "admin", admin_port, 32820, process_started=admin_process.append
            ) as admin,
            controller(services, root, args, "second", second_port, 32821) as second,
        ):
            paired = await admin.request(
                "commission_with_code", code=invitation["qrCode"], network_only=True, discovery_timeout_s=60
            )
            node = paired["node_id"]
            assert await attribute(admin, node, "0/40/2") == identity["vendor_id"]
            assert await attribute(admin, node, "0/40/4") == identity["product_id"]
            assert await attribute(admin, node, "0/40/15") == identity["serial_number"]
            servers = await attribute(admin, node, "1/29/1")
            clients = await attribute(admin, node, "1/29/2")
            parts = await attribute(admin, node, "2/29/3")
            assert {0x001D, 0x001E, 0x0039, 0x0551, 0x0553}.issubset(servers)
            assert {0x0550, 0x0552, WEBRTC_REQUESTOR}.isdisjoint(servers)
            assert clients == [WEBRTC_REQUESTOR] and parts == [1, 3]
            assert 0x0406 in await attribute(admin, node, "3/29/1")

            ticket = await admin.request(
                "open_commissioning_window", node_id=node, timeout=180, iteration=1000, option=1
            )
            second_node = (
                await second.request(
                    "commission_on_network",
                    setup_pin_code=ticket["setup_pin_code"],
                    ip_addr=args.ip,
                    discovery_timeout_s=60,
                )
            )["node_id"]
            assert not await read_bindings(admin, node, filtered=False)
            # These test targets exercise the registry independently of the
            # authenticated requestor used for WebRTC signaling.
            admin_target = await write_binding(admin, node, endpoint=1)
            second_target = await write_binding(second, second_node, endpoint=2)
            assert admin_target[-1] != second_target[-1]
            assert await read_bindings(admin, node, filtered=True) == [admin_target]
            assert await read_bindings(second, second_node, filtered=True) == [second_target]
            assert await read_bindings(admin, node, filtered=False) == sorted([admin_target, second_target])
            # A wildcard write uses one whole-list replacement, without list chunks.
            overflow = [
                {"1": admin_target[0], "3": endpoint, "4": WEBRTC_REQUESTOR, "254": admin_target[-1]}
                for endpoint in range(1, 33)
            ]
            reply = await admin.request("write_attribute", node_id=node, attribute_path="*/30/0", value=overflow)
            assert reply == [{"Path": {"EndpointId": 1, "ClusterId": 30, "AttributeId": 0}, "Status": 0x89}], reply
            assert await read_bindings(admin, node, filtered=False) == sorted([admin_target, second_target])

            async def decode(client, flow):
                # An accepted RTSP connection can precede its first authorized keyframe.
                async with asyncio.timeout(20):
                    while True:
                        try:
                            return await playback(client, node, 1, flow, 45)
                        except RuntimeError as error:
                            if "(Matter status 203)" not in str(error):
                                raise
                            await asyncio.sleep(0.25)

            first = await decode(admin, "receiver-offer")
            assert first["dimensions"] == [640, 360]
            await asyncio.to_thread(stop, device)

            before = snapshot.read_bytes()
            identity_file.write_text(json.dumps(dict(identity, serial_number="different-device")))
            await refuse("changed-durable-identity", "--allow-test-attestation")
            assert snapshot.read_bytes() == before
            identity_file.write_text(json.dumps(identity))
            device = boot("restarted", "--allow-test-attestation")
            await ready()
            assert await attribute(admin, node, "0/62/3") == 2
            assert await attribute(admin, node, "0/60/0") == 0
            assert await read_bindings(admin, node, filtered=False) == sorted([admin_target, second_target])
            assert await read_bindings(second, second_node, filtered=True) == [second_target]
            reply = await admin.request(
                "device_command", node_id=node, endpoint_id=0, cluster_id=62,
                command_name="RemoveFabric", payload={"fabricIndex": second_target[-1]},
            )
            assert reply["statusCode"] == 0
            assert await attribute(admin, node, "0/62/3") == 1
            assert await read_bindings(admin, node, filtered=False) == [admin_target]
            await asyncio.to_thread(stop, device)

        await asyncio.to_thread(stop, admin_process[0])
        # Synthesize the durable state between the two writes in RemoveFabric.
        # Only restore the old Binding record, keeping the fabric removal.
        clean = snapshot.read_bytes()
        old_start, old_end = binding_record(before)
        start_at, end_at = binding_record(clean)
        interrupted = clean[:start_at] + before[old_start:old_end] + clean[end_at:-32]
        snapshot.write_bytes(interrupted + sha256(interrupted).digest())
        device = boot("interrupted-removal-restart", "--allow-test-attestation")
        await ready()
        async with controller(
            services, root, args, "restored-admin", ports(1)[0], 32820, existing_storage=root / "admin"
        ) as restored:
            assert await attribute(restored, node, "0/62/3") == 1
            assert await read_bindings(restored, node, filtered=False) == [admin_target]
            repaired = snapshot.read_bytes()
            repaired_start, repaired_end = binding_record(repaired)
            assert repaired[repaired_start:repaired_end] == clean[start_at:end_at]
            recovered = await decode(restored, "camera-offer")
            assert recovered["dimensions"] == first["dimensions"]

    return dict(
        inadmissible_bundles=[
            "missing",
            "development_on_default_path",
            "exposed_key",
            "wrong_key",
            "altered_declaration",
            "wrong_product",
        ],
        new_state_untouched_on_rejection=True,
        provided_basic_information=True,
        changed_identity_preserved_durable_state=True,
        flows=["receiver-offer", "camera-offer"],
        frames=[first["decoded_frames"], recovered["decoded_frames"]],
        original_enrollment_restored=True,
        camera_server_clusters=servers,
        camera_client_clusters=clients,
        aggregator_parts=parts,
        binding_fabric_isolation=True,
        binding_overflow_preserves_entries=True,
        binding_restart_persistence=True,
        binding_selective_removal=True,
        binding_interrupted_removal_repaired=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "sdk", "controller", "device", "gateway", "publisher", "mediamtx", "ffmpeg"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--ip", required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    (args.runtime / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
