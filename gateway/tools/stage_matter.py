#!/usr/bin/env python3
"""Stage separate Matter media and detection credentials for an existing camera."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is not None


def stage(control: Path, directory: Path, camera_id: str, identity: str, microphone=False, recording=False):
    with os.fdopen(os.open(control, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.geteuid() or info.st_nlink != 1):
            raise ValueError("control configuration must be a private file owned by the operator")
        original = source.read(65537)
    if len(original) > 65536:
        raise ValueError("control configuration exceeds the gateway limit")
    config = json.loads(original)
    names = (
        (identity + "_media", "camera_source", "media.token"),
        (identity + "_detections", "detections", "detections.token"),
    )
    if microphone:
        names += ((identity + "_microphone", "microphone_control", "microphone.token"),)
    if recording:
        names += ((identity + "_recording", "recording_control", "recording.token"),)
    if not identifier(camera_id) or not identifier(identity) or not all(identifier(name) for name, _, _ in names):
        raise ValueError("invalid camera or service identity")
    cameras = [camera for camera in config["cameras"] if camera["camera_id"] == camera_id]
    if len(cameras) != 1 or not identifier(cameras[0]["site_id"]):
        raise ValueError("select one existing camera and its site")
    viewers = config["viewers"]
    if microphone and any(viewer.get("scope") == "microphone_control"
                          and camera_id in viewer["camera_ids"] for viewer in viewers):
        raise ValueError("camera already has a microphone controller")
    if recording and any(viewer.get("scope") == "recording_control"
                         and camera_id in viewer["camera_ids"] for viewer in viewers):
        raise ValueError("camera already has a recording controller")
    existing = {viewer["viewer_id"] for viewer in viewers}
    existing.update(publisher["username"] for publisher in config["publishers"])
    if len(viewers) + len(names) > 64 or any(name in existing for name, _, _ in names):
        raise ValueError("existing identity or insufficient enrollment capacity")

    tokens = {}
    for name, scope, filename in names:
        token = secrets.token_hex(32)
        tokens[filename] = token
        viewers.append(dict(viewer_id=name, scope=scope, site_id=cameras[0]["site_id"],
                            camera_ids=[camera_id], token_sha256=hashlib.sha256(token.encode()).hexdigest()))
    staged = (json.dumps(config, indent=2) + "\n").encode()
    if len(staged) > 65536:
        raise ValueError("staged configuration exceeds the gateway limit")

    directory.mkdir(mode=0o700)
    for filename, token in tokens.items():
        with os.fdopen(os.open(directory / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
            output.write(token + "\n")
    # An interrupted staging operation must not leave a usable control file.
    draft = directory / ".control.json"
    with os.fdopen(os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
        output.write(staged)
    draft.rename(directory / "control.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("control", type=Path, help="existing private gateway control JSON")
    parser.add_argument("directory", type=Path, help="new private staging directory")
    parser.add_argument("--camera", required=True, help="existing catalogue camera ID")
    parser.add_argument("--identity", required=True, help="prefix for the new service identities")
    parser.add_argument("--microphone", action="store_true", help="stage the camera's capture-control credential")
    parser.add_argument("--recording", action="store_true", help="stage the guarded recorder credential")
    args = parser.parse_args()
    try:
        stage(args.control, args.directory, args.camera, args.identity, args.microphone, args.recording)
    except (KeyError, TypeError, ValueError, OSError):
        parser.exit(1, "Matter staging failed. Check camera selection, service IDs and file permissions.\n")
    print("Prepared private Matter credentials and a staged copy of the gateway configuration.")


if __name__ == "__main__":
    main()
