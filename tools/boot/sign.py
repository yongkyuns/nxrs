#!/usr/bin/env python3
"""Offline, target-scoped MCUboot application signing; never flashes or provisions ROM keys."""
import argparse
from contextlib import contextmanager, redirect_stdout
import fcntl
import getpass
import hashlib
from importlib.metadata import version as package_version
import io
import json
import os
from pathlib import Path
import re
import select
import stat
import struct
import sys
import time
import warnings

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from imgtool.image import Image
from imgtool.keys.rsa import RSA, RSAPublic
from imgtool.version import decode_version

MAGIC = 0x96F3B83D
MANIFEST_TAG = 0xA0
MAX_STATE = 2 * 1024 * 1024
MAX_IMAGE = 16 * 1024 * 1024
MAX_RELEASES = 256
FORMAT = "mcuboot-rsa3072-pss-sha256"
DOMAIN = b"nxrs-firmware-authority-v1\0"


def require(value):
    if not value:
        raise ValueError("firmware signing validation failed")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode()


def parse(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result)
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique)


def exact(value, fields):
    require(isinstance(value, dict) and set(value) == set(fields.split()))


def number(value, low, high):
    require(type(value) is int and low <= value <= high)
    return value


def sha(data):
    return hashlib.sha256(data).hexdigest()


def hex_digest(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value))
    return value


def public_bytes(key):
    return key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def rsa_public(key):
    require(isinstance(key, rsa.RSAPublicKey) and key.key_size == 3072
            and key.public_numbers().e == 65537)
    return key


def envelope(body, key, purpose):
    signature = key.sign(DOMAIN + purpose + b"\0" + canonical(body),
                         padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                         hashes.SHA256())
    return dict(body, signature=signature.hex())


def open_envelope(data, key, purpose):
    require(isinstance(data, dict) and isinstance(data.get("signature"), str)
            and re.fullmatch(r"[0-9a-f]{768}", data["signature"]))
    body = {k: v for k, v in data.items() if k != "signature"}
    rsa_public(key).verify(bytes.fromhex(data["signature"]),
                           DOMAIN + purpose + b"\0" + canonical(body),
                           padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                           hashes.SHA256())
    return body


def validate_layout(layout):
    exact(layout, "flash_bytes primary_offset secondary_offset slot_bytes scratch_offset scratch_bytes erase_bytes header_bytes write_align max_align max_sectors")
    flash = number(layout["flash_bytes"], 64 * 1024, MAX_IMAGE)
    erase = number(layout["erase_bytes"], 4096, 65536)
    require(erase & (erase - 1) == 0)
    slot = number(layout["slot_bytes"], erase * 4, flash)
    scratch = number(layout["scratch_bytes"], erase, flash)
    ranges = []
    for offset, length in [(layout["primary_offset"], slot),
                           (layout["secondary_offset"], slot),
                           (layout["scratch_offset"], scratch)]:
        number(offset, erase, flash)
        require(offset % erase == length % erase == 0 and offset + length <= flash)
        ranges.append((offset, offset + length))
    ranges.sort()
    require(all(a[1] <= b[0] for a, b in zip(ranges, ranges[1:])))
    require(layout["write_align"] in (4, 8) and type(layout["write_align"]) is int)
    require(layout["max_align"] == 8 and type(layout["max_align"]) is int)
    require(type(layout["header_bytes"]) is int and layout["header_bytes"] in (32, 512, 1024, 4096))
    number(layout["max_sectors"], slot // erase, 4096)
    require(layout["header_bytes"] < slot)


def settings(value):
    exact(value, "schema target format configuration_sha256 layout")
    require(type(value["schema"]) is int and value["schema"] == 1 and value["format"] == FORMAT)
    require(isinstance(value["target"], str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value["target"]))
    hex_digest(value["configuration_sha256"])
    validate_layout(value["layout"])
    return value


def public_policy(data, pin):
    require(sha(data) == hex_digest(pin))
    value = parse(data)
    exact(value, "schema role settings public_key key_id signature")
    require(value["schema"] == 1 and type(value["schema"]) is int and value["role"] == "application-signing")
    settings(value["settings"])
    require(isinstance(value["public_key"], str) and len(value["public_key"]) < 4096)
    key = rsa_public(serialization.load_pem_public_key(value["public_key"].encode()))
    require(sha(public_bytes(key)) == hex_digest(value["key_id"]))
    open_envelope(value, key, b"policy")
    require(canonical(value) + b"\n" == data)
    return value, key


def private_stat(fd, limit=MAX_STATE, directory=False, links=(1,)):
    info = os.fstat(fd)
    require(info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0)
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
    if not directory:
        require(info.st_nlink in links and info.st_size <= limit)


def read_at(parent, name, limit, links=(1,)):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        private_stat(fd, limit, links=links)
        data = bytearray()
        while len(data) <= limit:
            part = os.read(fd, min(65536, limit + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        private_stat(fd, limit, links=links)
        require(len(data) <= limit)
        return bytes(data)
    finally:
        os.close(fd)


def write_new(parent, name, data):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    try:
        private_stat(fd, max(len(data), MAX_STATE))
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            require(written > 0)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def directory(path, lock=False):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts and path == path.resolve(strict=True))
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock_fd = None
    try:
        private_stat(fd, directory=True)
        if lock:
            lock_fd = os.open(".firmware.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                              0o600, dir_fd=fd)
            private_stat(lock_fd, 0)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield fd
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(fd)


def private_read(path, limit):
    path = Path(path)
    with directory(path.parent) as fd:
        return read_at(fd, path.name, limit)


def untrusted_read(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= limit)
        data = bytearray()
        while len(data) <= limit:
            part = os.read(fd, min(65536, limit + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        require(len(data) <= limit)
        return bytes(data)
    finally:
        os.close(fd)


def password(value):
    require(isinstance(value, bytes) and 32 <= len(value) <= 1024
            and b"\0" not in value and b"\n" not in value)
    return value


def read_password(fd):
    if fd is None:
        require(sys.stdin.isatty())
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return password(getpass.getpass("Offline firmware authority passphrase: ").encode())
    require(fd >= 3)
    try:
        info = os.fstat(fd)
        require(stat.S_ISFIFO(info.st_mode))
        if sys.platform == "linux":
            require(re.fullmatch(r"pipe:\[\d+\]", os.readlink(f"/proc/self/fd/{fd}")))
        elif sys.platform == "darwin":
            require(info.st_dev == 0 and info.st_nlink == 0)
        else:
            require(False)
        os.set_inheritable(fd, False)
        os.set_blocking(fd, False)
        data = bytearray()
        deadline = time.monotonic() + 2
        while True:
            remain = deadline - time.monotonic()
            require(remain > 0 and select.select([fd], [], [], remain)[0])
            try:
                part = os.read(fd, 1026 - len(data))
            except BlockingIOError:
                continue
            if not part:
                return password(bytes(data).removesuffix(b"\n"))
            data.extend(part)
            require(len(data) <= 1025)
    finally:
        os.close(fd)


def version_numbers(value):
    require(isinstance(value, str) and re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,5}\+\d{1,10}", value))
    values = [int(part) for part in re.split(r"[.+]", value)]
    for part, maximum in zip(values, [255, 255, 65535, 0xFFFFFFFF]):
        number(part, 0, maximum)
    require(value == f"{values[0]}.{values[1]}.{values[2]}+{values[3]}")
    return values


def manifest(policy, pin, body, version, counter):
    number(counter, 1, 0xFFFFFFFF)
    return {"schema": 1, "role": "nxrs-application-image", "policy_sha256": pin,
            "target": policy["settings"]["target"],
            "layout_sha256": sha(canonical(policy["settings"]["layout"])),
            "configuration_sha256": policy["settings"]["configuration_sha256"],
            "security_counter": counter, "version": version_numbers(version),
            "payload_sha256": sha(body)}


def produce(body, metadata, policy, key, fixed_signature=None):
    layout = policy["settings"]["layout"]
    require(0 < len(body) < layout["slot_bytes"] and body[:4] not in (b"\x7fELF", struct.pack("<I", MAGIC)))
    encoded = canonical(metadata)
    require(len(encoded) <= 1024)
    trailer = 3 * layout["max_sectors"] * layout["write_align"] + 48
    # Standard unencrypted RSA3072 TLVs: protected info/counter/custom header 16,
    # unprotected info/hash/key-hash/signature 464. Authorize size before signing.
    require(layout["header_bytes"] + len(body) + 16 + len(encoded) + 464 + trailer
            <= layout["slot_bytes"])
    version = metadata["version"]
    image = Image(version=decode_version(f"{version[0]}.{version[1]}.{version[2]}+{version[3]}"),
                  header_size=layout["header_bytes"], pad_header=True,
                  align=layout["write_align"], max_align=layout["max_align"],
                  slot_size=layout["slot_bytes"], max_sectors=layout["max_sectors"],
                  security_counter=metadata["security_counter"])
    image.infile_data = body
    image.payload = bytes([255]) * layout["header_bytes"] + body
    image.image_size = len(body)
    image.check_header()
    with redirect_stdout(io.StringIO()):
        if fixed_signature is None:
            image.create(RSA(key), "hash", None, custom_tlvs={MANIFEST_TAG: canonical(metadata)})
        else:
            image.create(None, "hash", None, pub_key=RSAPublic(key.public_key()),
                         fixed_sig={"value": bytes.fromhex(fixed_signature)},
                         custom_tlvs={MANIFEST_TAG: canonical(metadata)})
    image.check_trailer()
    return bytes(image.payload), image.signature.hex()


def tlvs(data, start, size, magic):
    require(size >= 4 and start + size <= len(data))
    require(struct.unpack_from("<HH", data, start) == (magic, size))
    values = {}
    offset = start + 4
    while offset < start + size:
        require(offset + 4 <= start + size)
        tag, length = struct.unpack_from("<HH", data, offset)
        offset += 4
        require(tag not in values and length > 0 and offset + length <= start + size)
        values[tag] = data[offset:offset + length]
        offset += length
    require(offset == start + size)
    return values


def verify_image(data, policy_data, pin, target, configuration, minimum_counter):
    policy, key = public_policy(policy_data, pin)
    config = policy["settings"]
    require(config["target"] == target and config["configuration_sha256"] == hex_digest(configuration))
    layout = config["layout"]
    require(32 <= len(data) <= layout["slot_bytes"])
    magic, load, header, protected, body_len, flags, major, minor, revision, build, zero = struct.unpack_from("<IIHHIIBBHII", data)
    require(magic == MAGIC and load == flags == zero == 0 and header == layout["header_bytes"])
    require(data[32:header] == bytes([255]) * (header - 32) and body_len > 0)
    protected_start = header + body_len
    protected_values = tlvs(data, protected_start, protected, 0x6908)
    require(set(protected_values) == {0x50, MANIFEST_TAG} and len(protected_values[0x50]) == 4)
    authenticated_end = protected_start + protected
    require(authenticated_end + 4 <= len(data))
    unprotected_size = struct.unpack_from("<H", data, authenticated_end + 2)[0]
    require(authenticated_end + unprotected_size == len(data))
    values = tlvs(data, authenticated_end, unprotected_size, 0x6907)
    require(set(values) == {0x10, 0x01, 0x23} and len(values[0x23]) == 384)
    require(values[0x10] == hashlib.sha256(data[:authenticated_end]).digest())
    pkcs1 = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.PKCS1)
    require(values[0x01] == hashlib.sha256(pkcs1).digest())
    key.verify(values[0x23], data[:authenticated_end],
               padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
    require(len(protected_values[MANIFEST_TAG]) <= 1024)
    metadata = parse(protected_values[MANIFEST_TAG])
    counter = struct.unpack("<I", protected_values[0x50])[0]
    version = f"{major}.{minor}.{revision}+{build}"
    expected = manifest(policy, pin, data[header:protected_start], version, counter)
    require(metadata == expected and protected_values[MANIFEST_TAG] == canonical(expected))
    number(minimum_counter, 1, 0xFFFFFFFF)
    require(counter >= minimum_counter)
    # Reserve the exact upstream scratch-swap trailer budget; accept no preconfirmation/padding.
    trailer = 3 * layout["max_sectors"] * layout["write_align"] + 48
    require(len(data) + trailer <= layout["slot_bytes"])
    return metadata


def initialize(path, configuration, passphrase):
    password(passphrase)
    settings(configuration)
    with directory(path, lock=True) as fd:
        require(set(os.listdir(fd)) == {".firmware.lock"})
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        body = {"schema": 1, "role": "application-signing", "settings": configuration,
                "public_key": key.public_key().public_bytes(serialization.Encoding.PEM,
                    serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
                "key_id": sha(public_bytes(key.public_key()))}
        policy = canonical(envelope(body, key, b"policy")) + b"\n"
        encrypted = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.BestAvailableEncryption(passphrase)).decode()
        state = {"schema": 1, "generation": 0, "policy_sha256": sha(policy),
                 "encrypted_key": encrypted, "maximum_issued_counter": 0, "releases": []}
        # State is durable before the public policy appears; partial init fails closed.
        write_new(fd, "state.json", canonical(envelope(state, key, b"state")) + b"\n")
        os.fsync(fd)
        write_new(fd, "policy.json", policy)
        os.fsync(fd)
        return sha(policy)


def state_open(fd, passphrase, pin):
    policy_data = read_at(fd, "policy.json", 8192)
    policy, public = public_policy(policy_data, pin)
    raw = parse(read_at(fd, "state.json", MAX_STATE))
    state = validate_state(raw, public, policy, pin)
    key = serialization.load_pem_private_key(state["encrypted_key"].encode(), password(passphrase))
    require(isinstance(key, rsa.RSAPrivateKey) and public_bytes(key.public_key()) == public_bytes(public))
    return policy_data, policy, key, state


def validate_state(raw, public, policy, pin):
    state = open_envelope(raw, public, b"state")
    exact(state, "schema generation policy_sha256 encrypted_key maximum_issued_counter releases")
    require(type(state["schema"]) is int and state["schema"] == 1 and state["policy_sha256"] == pin)
    require(isinstance(state["encrypted_key"], str) and len(state["encrypted_key"]) < 8192
            and state["encrypted_key"].startswith("-----BEGIN ENCRYPTED PRIVATE KEY-----\n"))
    number(state["generation"], 0, MAX_RELEASES)
    number(state["maximum_issued_counter"], 0, 0xFFFFFFFF)
    require(isinstance(state["releases"], list) and len(state["releases"]) == state["generation"])
    require(len(state["releases"]) <= MAX_RELEASES)
    for entry in state["releases"]:
        exact(entry, "request_id metadata image_signature image_sha256 image_bytes")
        require(entry["request_id"] == sha(canonical(entry["metadata"])))
        hex_digest(entry["image_sha256"])
        number(entry["image_bytes"], 32, MAX_IMAGE)
        require(isinstance(entry["image_signature"], str) and re.fullmatch(r"[0-9a-f]{768}", entry["image_signature"]))
    require(len({e["request_id"] for e in state["releases"]}) == len(state["releases"]))
    counters = []
    for entry in state["releases"]:
        metadata = entry["metadata"]
        exact(metadata, "schema role policy_sha256 target layout_sha256 configuration_sha256 security_counter version payload_sha256")
        require(type(metadata["schema"]) is int and metadata["schema"] == 1
                and metadata["role"] == "nxrs-application-image" and metadata["policy_sha256"] == pin)
        require(metadata["target"] == policy["settings"]["target"]
                and metadata["configuration_sha256"] == policy["settings"]["configuration_sha256"]
                and metadata["layout_sha256"] == sha(canonical(policy["settings"]["layout"])))
        require(isinstance(metadata["version"], list) and len(metadata["version"]) == 4)
        for part, maximum in zip(metadata["version"], [255, 255, 65535, 0xFFFFFFFF]):
            number(part, 0, maximum)
        hex_digest(metadata["payload_sha256"])
        counters.append(number(metadata["security_counter"], 1, 0xFFFFFFFF))
    require(counters == sorted(counters)
            and state["maximum_issued_counter"] == (counters[-1] if counters else 0))
    return state


def commit_state(fd, state, key):
    data = canonical(envelope(state, key, b"state")) + b"\n"
    require(len(data) <= MAX_STATE)
    # A leftover journal is never ignored or overwritten. Explicit recovery is required.
    write_new(fd, ".state-next", data)
    os.fsync(fd)
    os.replace(".state-next", "state.json", src_dir_fd=fd, dst_dir_fd=fd)
    os.fsync(fd)


def publish(path, data):
    path = Path(path)
    require(path.name not in ("", ".", "..") and not path.name.startswith(".nxrs-release-"))
    # The private directory reserves this exact content-addressed staging namespace.
    name = ".nxrs-release-" + sha(data)
    with directory(path.parent, lock=True) as fd:
        try:
            existing = read_at(fd, path.name, MAX_IMAGE, links=(1, 2))
        except FileNotFoundError:
            existing = None
        if existing is not None:
            require(existing == data)
            info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            if info.st_nlink == 2:
                staged = os.stat(name, dir_fd=fd, follow_symlinks=False)
                require((info.st_dev, info.st_ino) == (staged.st_dev, staged.st_ino)
                        and staged.st_nlink == 2)
                require(read_at(fd, name, MAX_IMAGE, links=(2,)) == data)
                # Recover the exact alias left by interruption after link publication.
                os.unlink(name, dir_fd=fd)
            else:
                require(info.st_nlink == 1)
            os.fsync(fd)
            return
        try:
            staged = read_at(fd, name, MAX_IMAGE)
        except FileNotFoundError:
            staged = None
        if staged is not None and staged != data:
            # Only an exact incomplete prefix in the reserved, unlinked staging name
            # can be reconstructed from the already committed immutable signature.
            require(len(staged) < len(data) and data.startswith(staged))
            os.unlink(name, dir_fd=fd)
            os.fsync(fd)
            staged = None
        if staged is None:
            write_new(fd, name, data)
        else:
            handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                private_stat(handle, MAX_IMAGE)
                os.fsync(handle)
            finally:
                os.close(handle)
        # Atomic no-replace publication. No failure path unlinks an unknown file.
        os.link(name, path.name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        os.unlink(name, dir_fd=fd)
        os.fsync(fd)


def sign(path, passphrase, pin, body, version, counter, output, payload_sha256):
    require(sha(body) == hex_digest(payload_sha256))
    require(Path(path) not in Path(output).parents)
    with directory(path, lock=True) as fd:
        policy_data, policy, key, state = state_open(fd, passphrase, pin)
        require(".state-next" not in os.listdir(fd))
        require(counter >= state["maximum_issued_counter"])
        metadata = manifest(policy, pin, body, version, counter)
        request_id = sha(canonical(metadata))
        entries = [e for e in state["releases"] if e["request_id"] == request_id]
        prior = entries[0] if entries else None
        if prior is None:
            require(len(state["releases"]) < MAX_RELEASES)
        data, signature = produce(body, metadata, policy, key, prior["image_signature"] if prior else None)
        verify_image(data, policy_data, pin, policy["settings"]["target"],
                     policy["settings"]["configuration_sha256"], counter)
        entry = {"request_id": request_id, "metadata": metadata, "image_signature": signature,
                 "image_sha256": sha(data), "image_bytes": len(data)}
        if prior:
            require(entry == prior)
        else:
            state["releases"].append(entry)
            state["generation"] += 1
            state["maximum_issued_counter"] = counter
            commit_state(fd, state, key)
        # Retry must also finish any interrupted parent-directory durability barrier.
        os.fsync(fd)
        # Every signature is durably reserved before any export path is published.
        publish(output, data)
        return entry["image_sha256"]


def export_public(policy_data, pin, target, configuration, output):
    policy, _ = public_policy(policy_data, pin)
    require(policy["settings"]["target"] == target
            and policy["settings"]["configuration_sha256"] == hex_digest(configuration))
    publish(output, policy["public_key"].encode())


def recover(path, passphrase, pin, discard_incomplete=False):
    with directory(path, lock=True) as fd:
        _, policy, key, current = state_open(fd, passphrase, pin)
        try:
            data = read_at(fd, ".state-next", MAX_STATE)
        except FileNotFoundError:
            return
        try:
            raw = parse(data)
            open_envelope(raw, key.public_key(), b"state")
        except (ValueError, TypeError, KeyError, InvalidSignature):
            require(discard_incomplete)
            # An uncommitted, unauthenticated journal cannot publish an image.
            os.unlink(".state-next", dir_fd=fd)
            os.fsync(fd)
            return
        pending = validate_state(raw, key.public_key(), policy, pin)
        require(pending["encrypted_key"] == current["encrypted_key"])
        require(pending == current or (
            pending["generation"] == current["generation"] + 1
            and pending["releases"][:-1] == current["releases"]))
        require(not discard_incomplete)
        if pending == current:
            os.unlink(".state-next", dir_fd=fd)
        else:
            os.replace(".state-next", "state.json", src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--authority", type=Path, required=True)
    init.add_argument("--settings", type=Path, required=True)
    init.add_argument("--password-fd", type=int)
    issue = sub.add_parser("sign")
    issue.add_argument("--authority", type=Path, required=True)
    issue.add_argument("--policy-sha256", required=True)
    issue.add_argument("--input", type=Path, required=True)
    issue.add_argument("--output", type=Path, required=True)
    issue.add_argument("--payload-sha256", required=True)
    issue.add_argument("--version", required=True)
    issue.add_argument("--security-counter", type=int, required=True)
    issue.add_argument("--password-fd", type=int)
    public = sub.add_parser("public-key")
    public.add_argument("--policy", type=Path, required=True)
    public.add_argument("--policy-sha256", required=True)
    public.add_argument("--target", required=True)
    public.add_argument("--configuration-sha256", required=True)
    public.add_argument("--output", type=Path, required=True)
    recovery = sub.add_parser("recover-state")
    recovery.add_argument("--authority", type=Path, required=True)
    recovery.add_argument("--policy-sha256", required=True)
    recovery.add_argument("--password-fd", type=int)
    recovery.add_argument("--discard-incomplete", action="store_true")
    verify = sub.add_parser("verify")
    verify.add_argument("--policy", type=Path, required=True)
    verify.add_argument("--policy-sha256", required=True)
    verify.add_argument("--image", type=Path, required=True)
    verify.add_argument("--target", required=True)
    verify.add_argument("--configuration-sha256", required=True)
    verify.add_argument("--minimum-security-counter", type=int, required=True)
    args = parser.parse_args()
    try:
        require(sys.version_info >= (3, 12) and sys.byteorder == "little"
                and sys.platform in ("linux", "darwin"))
        require(package_version("imgtool") == "2.4.0" and package_version("cryptography") == "50.0.2")
        if args.command == "init":
            pin = initialize(args.authority, parse(untrusted_read(args.settings, 8192)), read_password(args.password_fd))
            print("Firmware public policy SHA-256:", pin)
        elif args.command == "sign":
            result = sign(args.authority, read_password(args.password_fd), args.policy_sha256,
                          untrusted_read(args.input, MAX_IMAGE), args.version, args.security_counter, args.output, args.payload_sha256)
            print("Signed application image SHA-256:", result)
        elif args.command == "public-key":
            export_public(untrusted_read(args.policy, 8192), args.policy_sha256,
                          args.target, args.configuration_sha256, args.output)
            print("Verified application public key exported")
        elif args.command == "recover-state":
            recover(args.authority, read_password(args.password_fd), args.policy_sha256, args.discard_incomplete)
            print("Firmware authority journal recovery completed")
        else:
            verify_image(untrusted_read(args.image, MAX_IMAGE), untrusted_read(args.policy, 8192),
                         args.policy_sha256, args.target, args.configuration_sha256, args.minimum_security_counter)
            print("Application image signature and explicit policy checks passed")
    except Exception:
        print("Firmware authority operation failed; no activation is performed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
