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
    parser.add_argument("--variant", choices=["camera", "bridge", "rtsp"], default="camera",
                        help="select the camera example, bridged occupancy, or bridged RTSP input")
    args = parser.parse_args()
    sdk = args.sdk.resolve(strict=True)
    pins = json.loads((FIXTURE / "versions.json").read_text())
    sdk_patches = ["rs-matter-camera.patch"]
    if args.variant in ("bridge", "rtsp"):
        sdk_patches.append("bridge-topology.patch")
    if args.variant == "rtsp":
        sdk_patches.append("live-media.patch")
    for name in (*sdk_patches, "str0m-rejected-media.patch", "Cargo.lock"):
        if hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() != pins["sha256"][name]:
            raise SystemExit("fixture digest mismatch: " + name)
    if hashlib.sha256(args.webrtc_archive.read_bytes()).hexdigest() != pins["str0m"]["archive_sha256"]:
        raise SystemExit("WebRTC crate archive does not match the fixture pin")
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
    subprocess.run([*git, "apply", "--directory=" + engine,
                    str(FIXTURE / "str0m-rejected-media.patch")], check=True)
    for name in sdk_patches:
        subprocess.run([*git, "apply", str(FIXTURE / name)], check=True)
    # Ordinary declared Cargo dependency. Its path is local build input, not a
    # committed absolute path or production dependency-selection environment switch.
    manifest = sdk / "examples/Cargo.toml"
    source = manifest.read_text()
    declaration = "nxrs-gateway-matter = { path = " + json.dumps(str(ROOT / "gateway/matter")) + " }\n"
    declaration += 'tokio = { workspace = true, features = ["rt", "net", "time"] }\n'
    if source.count("[dependencies]\n") != 1:
        raise SystemExit("unexpected SDK examples manifest")
    manifest.write_text(source.replace("[dependencies]\n", "[dependencies]\n" + declaration))
    with (sdk / "Cargo.toml").open("a") as workspace:
        workspace.write('\n[patch.crates-io]\nstr0m = { path = ' + json.dumps(engine) + ' }\n')
    shutil.copyfile(FIXTURE / "Cargo.lock", sdk / "Cargo.lock")
    print("Prepared pinned Matter fixture:", args.variant)


if __name__ == "__main__":
    main()
