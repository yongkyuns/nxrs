#!/usr/bin/env python3
"""Exercise the two root TLS management clusters with independent Matter controllers."""
import argparse
import asyncio
import base64
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from matter_attestation_reference import public_test_bundle
from matter_commissioning_reference import provision
from matter_fabric_reference import attribute, controller
from matter_gateway_reference import start, stop
from matter_rtsp_reference import ports, publish, start_media, wait_path
from matter_source_access import private_file
from matter_time_reference import receive, records, time_command


CERTIFICATES, ENDPOINTS = 2049, 2050


def octets(value):
    return base64.b64encode(value).decode("ascii")


async def command(client, node, cluster, name, **fields):
    return await client.request(
        "device_command", node_id=node, endpoint_id=0, cluster_id=cluster, command_name=name, payload=fields,
    )


async def rejected(invoke, code, *, cluster=False):
    expected = f"({'cluster' if cluster else 'Matter'} status {code})"
    try:
        await invoke
    except RuntimeError as error:
        if expected not in str(error):
            raise
    else:
        raise AssertionError(f"expected rejection with {expected}")


def certificate(builder, subject, issuer, public, signer, *, is_ca, padding=0):
    now = datetime.now(timezone.utc)
    builder = (builder.subject_name(subject).issuer_name(issuer).public_key(public)
               .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
               .not_valid_after(now + timedelta(days=30))
               .add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), critical=True))
    if padding:
        builder = builder.add_extension(
            x509.UnrecognizedExtension(x509.ObjectIdentifier("1.3.6.1.4.1.55555.1"), b"x" * padding),
            critical=False,
        )
    return builder.sign(signer, hashes.SHA256()).public_bytes(serialization.Encoding.DER)


async def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    bundle = root / "attestation"
    public_test_bundle(args.sdk, bundle)
    factory = provision(root / "factory", args.controller)
    invitation = json.loads((factory.parent / "onboarding.json").read_bytes())
    state = root / "state"
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
            process = start(
                services, root, label, [args.device, "--config", config, "--allow-test-attestation", *flags],
                dict(os.environ, RUST_LOG="warn", TMPDIR=str(root)),
            )
            await asyncio.to_thread(
                wait_path, gateway, lambda path: path.get("ready") and len(path["readers"]) == 1,
                (process, media, encoder, publisher),
            )
            return process

        device = await boot("initial", "--initialize-state", "--open-commissioning-window")
        a_port, b_port = ports(2)
        async with (
            controller(services, root, args, "admin", a_port, 33830) as a,
            controller(services, root, args, "secondary", b_port, 33831) as b,
        ):
            aid = (await a.request(
                "commission_with_code", code=invitation["qrCode"], network_only=True, discovery_timeout_s=60,
            ))["node_id"]
            assert {CERTIFICATES, ENDPOINTS}.issubset(await attribute(a, aid, "0/29/1"))
            assert {CERTIFICATES, ENDPOINTS}.isdisjoint(await attribute(a, aid, "1/29/1"))
            assert await attribute(a, aid, "0/2049/0") == 5
            assert await attribute(a, aid, "0/2049/2") == 2
            assert await attribute(a, aid, "0/2050/0") == 5
            ticket = await a.request("open_commissioning_window", node_id=aid, timeout=180, iteration=1000, option=1)
            bid = (await b.request(
                "commission_on_network", setup_pin_code=ticket["setup_pin_code"],
                ip_addr=args.ip, discovery_timeout_s=60,
            ))["node_id"]
            b_index = await attribute(b, bid, "0/62/5")

            def first(cluster, name, **fields):
                return command(a, aid, cluster, name, **fields)

            def second(cluster, name, **fields):
                return command(b, bid, cluster, name, **fields)

            await time_command(a, aid, "SetUTCTime", utcTime=(int(time.time()) - 946684800) * 1_000_000, granularity=3)
            key = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "Nxrs qualification CA")])
            der = certificate(x509.CertificateBuilder(), name, name, key.public_key(), key,
                              is_ca=True, padding=1750 if args.large_root else 0)
            a_ca = (await first(CERTIFICATES, "ProvisionRootCertificate", caid=None, certificate=octets(der)))["caid"]
            await rejected(second(CERTIFICATES, "FindRootCertificate", caid=a_ca), 139)
            b_ca = (await second(CERTIFICATES, "ProvisionRootCertificate", caid=None, certificate=octets(der)))["caid"]
            assert a_ca != b_ca
            assert (await first(CERTIFICATES, "LookupRootCertificate", fingerprint=octets(sha256(der).digest())))["caid"] == a_ca
            root_details = (await first(CERTIFICATES, "FindRootCertificate", caid=a_ca))["certificateDetails"]
            assert base64.b64decode(root_details[0]["certificate"]) == der

            nonce = bytes(range(32))
            csr = await first(CERTIFICATES, "ClientCSR", ccdid=None, nonce=octets(nonce))
            ccdid = csr["ccdid"]
            request = x509.load_der_x509_csr(base64.b64decode(csr["csr"]))
            assert request.is_signature_valid
            proof = base64.b64decode(csr["nonceSignature"])
            assert len(proof) == 64
            signature = encode_dss_signature(int.from_bytes(proof[:32]), int.from_bytes(proof[32:]))
            request.public_key().verify(signature, nonce, ec.ECDSA(hashes.SHA256()))
            leaf = certificate(x509.CertificateBuilder(), request.subject, name, request.public_key(), key, is_ca=False)
            await first(CERTIFICATES, "ProvisionClientCertificate", ccdid=ccdid,
                        clientCertificate=octets(leaf), intermediateCertificates=[])
            assert (await first(CERTIFICATES, "LookupClientCertificate", fingerprint=octets(sha256(leaf).digest())))["ccdid"] == ccdid
            client_details = (await first(CERTIFICATES, "FindClientCertificate", ccdid=ccdid))["certificateDetails"]
            assert base64.b64decode(client_details[0]["clientCertificate"]) == leaf
            await rejected(second(CERTIFICATES, "FindClientCertificate", ccdid=ccdid), 139)
            b_ccdid = (await second(CERTIFICATES, "ClientCSR", ccdid=None, nonce=octets(nonce)))["ccdid"]

            settings = dict(hostname=octets(b"camera-test.example"), port=443, ccdid=ccdid, endpointId=None)
            await rejected(second(ENDPOINTS, "ProvisionEndpoint", caid=a_ca, **settings), 3, cluster=True)
            await rejected(second(ENDPOINTS, "ProvisionEndpoint", caid=b_ca, **settings), 4, cluster=True)
            endpoint = (await first(ENDPOINTS, "ProvisionEndpoint", caid=a_ca, **settings))["endpointId"]
            await rejected(first(ENDPOINTS, "ProvisionEndpoint", caid=a_ca, **settings), 2, cluster=True)
            b_endpoint = (await second(ENDPOINTS, "ProvisionEndpoint", caid=b_ca,
                                       **{**settings, "ccdid": b_ccdid}))["endpointId"]
            assert b_endpoint != endpoint
            await rejected(second(ENDPOINTS, "FindEndpoint", endpointId=endpoint), 139)
            await rejected(first(CERTIFICATES, "RemoveRootCertificate", caid=a_ca), 203)
            await rejected(first(CERTIFICATES, "RemoveClientCertificate", ccdid=ccdid), 203)

            await asyncio.to_thread(stop, device)
            device = await boot("restarted")
            found = (await first(ENDPOINTS, "FindEndpoint", endpointId=endpoint))["endpoint"]
            assert found["caid"] == a_ca and found["ccdid"] == ccdid and found["referenceCount"] == 0
            assert base64.b64decode(found["hostname"]) == b"camera-test.example"
            await first(ENDPOINTS, "ProvisionEndpoint", caid=a_ca, **{**settings, "port": 8443, "endpointId": endpoint})
            assert (await first(ENDPOINTS, "FindEndpoint", endpointId=endpoint))["endpoint"]["port"] == 8443
            path = "0/2050/1"
            filtered = await a.request("read_attribute", node_id=aid, attribute_path=path, fabric_filtered=True)
            all_visible = await a.request("read_attribute", node_id=aid, attribute_path=path, fabric_filtered=False)
            assert {entry["0"] for entry in filtered[path]} == {endpoint}
            assert {entry["0"] for entry in all_visible[path]} == {endpoint, b_endpoint}
            removal = await first(62, "RemoveFabric", fabricIndex=b_index)
            assert removal["statusCode"] == 0
            retired = records((state / "state.bin").read_bytes())
            assert 0x1100 + b_index not in retired and 0x1400 + b_index not in retired
            b_slot = 0x1201 + (b_index - 1) * 2
            assert b_slot not in retired and b_slot + 1 not in retired

            await first(ENDPOINTS, "RemoveEndpoint", endpointId=endpoint)
            await first(CERTIFICATES, "RemoveClientCertificate", ccdid=ccdid)
            await first(CERTIFICATES, "RemoveRootCertificate", caid=a_ca)
            assert await attribute(a, aid, "0/2050/1") == []
            frames = await receive(a, aid)
            await asyncio.to_thread(stop, device)
    return dict(root_bytes=len(der), separate_fabrics=True, certificate_references_protected=True,
                endpoint_persisted_and_updated=True, fabric_records_retired_together=True, decoded_frames=frames)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "sdk", "controller", "device", "gateway", "publisher", "mediamtx", "ffmpeg"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--ip", required=True)
    parser.add_argument("--large-root", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    (args.runtime / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
