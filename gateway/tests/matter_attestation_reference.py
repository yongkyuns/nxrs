#!/usr/bin/env python3
"""Exercise protected, file-provisioned Matter identity with the pinned SDK's public test chain."""

import argparse
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import socket
import subprocess

from matter_commissioning_reference import provision
from matter_fabric_reference import attribute, controller
from matter_gateway_reference import start, stop
from matter_reference import playback
from matter_rtsp_reference import ports, publish, start_media, wait_path
from matter_source_access import private_file


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
        async with controller(services, root, args, "admin", ports(1)[0], 32820) as admin:
            paired = await admin.request(
                "commission_with_code", code=invitation["qrCode"], network_only=True, discovery_timeout_s=60
            )
            node = paired["node_id"]
            assert await attribute(admin, node, "0/40/2") == identity["vendor_id"]
            assert await attribute(admin, node, "0/40/4") == identity["product_id"]
            assert await attribute(admin, node, "0/40/15") == identity["serial_number"]

            async def decode():
                # An accepted RTSP connection can precede its first authorized keyframe.
                async with asyncio.timeout(20):
                    while True:
                        try:
                            return await playback(admin, node, 1, "receiver-offer", 45)
                        except RuntimeError as error:
                            if "(Matter status 203)" not in str(error):
                                raise
                            await asyncio.sleep(0.25)

            first = await decode()
            assert first["dimensions"] == [640, 360]
            await asyncio.to_thread(stop, device)

            before = (state / "state.bin").read_bytes()
            identity_file.write_text(json.dumps(dict(identity, serial_number="different-device")))
            await refuse("changed-durable-identity", "--allow-test-attestation")
            assert (state / "state.bin").read_bytes() == before
            identity_file.write_text(json.dumps(identity))
            device = boot("restarted", "--allow-test-attestation")
            await ready()
            assert await attribute(admin, node, "0/62/3") == 1
            assert await attribute(admin, node, "0/60/0") == 0
            recovered = await decode()
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
        frames=[first["decoded_frames"], recovered["decoded_frames"]],
        original_enrollment_restored=True,
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
