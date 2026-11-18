#!/usr/bin/env python3
"""Prepare the pinned external Matter fixture; never downloads or starts a service."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "gateway/tests/matter"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True, help="fresh clean rs-matter checkout")
    parser.add_argument("--webrtc-archive", type=Path, required=True,
                        help="published str0m crate archive; checked against its registry checksum")
    parser.add_argument("--dnssd-archive", type=Path, help="published astro-dnssd crate for scoped LAN builds")
    variants = ("camera", "bridge", "rtsp", "audio-control", "audio-playback", "privacy", "commissioning", "viewer-permissions", "source-admission", "catalogue", "sessions", "snapshots", "shutdown", "final-fabric", "commissioning-recovery", "shutdown-drain", "lan-interface", "stream-continuity", "provisioned-attestation", "camera-endpoints", "root-power-time-zone")
    parser.add_argument("--variant", choices=variants, default="camera")
    args = parser.parse_args()
    sdk = args.sdk.resolve(strict=True)
    pins = json.loads((FIXTURE / "versions.json").read_text())
    # Each variant builds on the preceding fixture, in patch application order.
    patches = ("rs-matter-camera.patch", "bridge-topology.patch", "live-media.patch",
               "audio-control.patch", "audio-playback.patch", "privacy.patch", "commissioning.patch", "viewer-permissions.patch", "source-admission.patch", "catalogue.patch", "sessions.patch", "snapshots.patch", "shutdown.patch", "final-fabric.patch", "commissioning-recovery.patch", "shutdown-drain.patch", "lan-interface.patch", "stream-continuity.patch", "provisioned-attestation.patch", "camera-endpoints.patch", "root-power-time-zone.patch")
    sdk_patches = patches[:variants.index(args.variant) + 1]
    engine_patches = ["str0m-rejected-media.patch"]
    if "audio-playback.patch" in sdk_patches:
        engine_patches.append("str0m-sync.patch")
    scoped_dns = "lan-interface.patch" in sdk_patches
    dns_patch = ("astro-dnssd-interface.patch",) if scoped_dns else ()
    for name in (*sdk_patches, *engine_patches, *dns_patch, "Cargo.lock"):
        if hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() != pins["sha256"][name]:
            raise SystemExit("fixture digest mismatch: " + name)
    if hashlib.sha256(args.webrtc_archive.read_bytes()).hexdigest() != pins["str0m"]["archive_sha256"]:
        raise SystemExit("WebRTC crate archive does not match the fixture pin")
    if scoped_dns and (
        args.dnssd_archive is None
        or hashlib.sha256(args.dnssd_archive.read_bytes()).hexdigest() != pins["astro_dnssd"]["archive_sha256"]
    ):
        raise SystemExit("The selected variant requires the checksum-verified --dnssd-archive")
    git = ["git", "-C", str(sdk)]
    revision = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    if revision != pins["rs_matter"]["revision"]:
        raise SystemExit("SDK revision does not match the fixture pin")
    if subprocess.check_output([*git, "status", "--porcelain"], text=True).strip():
        raise SystemExit("SDK checkout must be clean; use a fresh isolated clone")
    subprocess.run([*git, "apply", "--check", str(FIXTURE / "rs-matter-camera.patch")], check=True)
    # Patch an isolated, checksum-verified published crate, never Cargo's shared
    # source cache. Its normalized manifest retains the registry dependency pins.
    engine = "nxrs-fixture-deps/str0m-" + pins["str0m"]["version"]
    with tarfile.open(args.webrtc_archive) as archive:
        archive.extractall(sdk / "nxrs-fixture-deps", filter="data")
    for name in engine_patches:
        subprocess.run([*git, "apply", "--directory=" + engine, str(FIXTURE / name)], check=True)
    for name in sdk_patches:
        subprocess.run([*git, "apply", str(FIXTURE / name)], check=True)
    # Ordinary declared Cargo dependency. Its path is local build input, not a
    # committed absolute path or production dependency-selection environment switch.
    manifest = sdk / "examples/Cargo.toml"
    source = manifest.read_text()
    declaration = "nxrs-gateway-matter = { path = " + json.dumps(str(ROOT / "gateway/matter")) + " }\n"
    declaration += 'tokio = { workspace = true, features = ["rt", "net", "time"] }\n'
    declaration += 'futures-util = { version = "=0.3.34", default-features = false, features = ["alloc"] }\n'
    if source.count("[dependencies]\n") != 1:
        raise SystemExit("unexpected SDK examples manifest")
    manifest.write_text(source.replace("[dependencies]\n", "[dependencies]\n" + declaration))
    with (sdk / "Cargo.toml").open("a") as workspace:
        workspace.write('\n[patch.crates-io]\nstr0m = { path = ' + json.dumps(engine) + ' }\n')
        if scoped_dns:
            dns_pin = pins["astro_dnssd"]
            dns = "nxrs-fixture-deps/astro-dnssd-" + dns_pin["version"]
            with tarfile.open(args.dnssd_archive) as archive:
                archive.extractall(sdk / "nxrs-fixture-deps", filter="data")
            subprocess.run([*git, "apply", "--directory=" + dns, str(FIXTURE / dns_patch[0])], check=True)
            workspace.write("astro-dnssd = { path = " + json.dumps(dns) + " }\n")
    if scoped_dns:
        lock = (FIXTURE / "Cargo.lock").read_text()
        pkg = f'name = "astro-dnssd"\nversion = "{dns_pin["version"]}"\n'
        registry_record = pkg + 'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
        registry_record += f'checksum = "{dns_pin["archive_sha256"]}"\n'
        if lock.count(registry_record) != 1:
            raise SystemExit("unexpected DNS-SD lock entry")
        (sdk / "Cargo.lock").write_text(lock.replace(registry_record, pkg))
    else:
        shutil.copyfile(FIXTURE / "Cargo.lock", sdk / "Cargo.lock")
    print("Prepared pinned Matter fixture:", args.variant)


if __name__ == "__main__":
    main()
