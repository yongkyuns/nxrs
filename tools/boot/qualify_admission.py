#!/usr/bin/env python3
"""Actual Rust admission + frozen native SDK qualification; no device operations."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import struct
import sys

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import build_esp32s3 as builder
import qualify as process
import sign
import target_plan as target
import native_inputs

ROOT = target.ROOT
FIXTURE = ROOT / "tests/boot/admission"
PROFILE = "esp32s3-camera-mcuboot"

CRYPTO_MODULES = {"MBEDTLS_BIGNUM_C", "MBEDTLS_OID_C", "MBEDTLS_RSA_C", "MBEDTLS_ASN1_PARSE_C",
                  "MBEDTLS_ASN1_WRITE_C", "MBEDTLS_MD_C", "MBEDTLS_SHA256_C"}


def rust_bytes(raw):
    return "[" + ",".join(str(byte) for byte in raw) + "]"


def tlv(tag, value):
    return struct.pack("<HH", tag, len(value)) + value


def fixtures(work):
    settings = target.plan(PROFILE)["settings"]
    authority = work / "authority"
    authority.mkdir(mode=0o700)
    password = os.urandom(48).hex().encode("ascii")
    pin = sign.initialize(authority, settings, password)
    with sign.directory(authority) as fd:
        public_data, policy, key, _ = sign.state_open(fd, password, pin)
    del password
    body = b"nxrs-admission-synthetic-body\0" + bytes(range(256)) * 16
    metadata = sign.manifest(policy, pin, body, "2.3.4+5", 7)
    valid, _ = sign.produce(body, metadata, policy, key)
    key_hash = hashlib.sha256(key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.PKCS1)).digest()
    cases = []

    def add(label, raw, expected, native=False, floor=1):
        path = work / (label + ".bin")
        path.write_bytes(raw)
        signed_end = struct.unpack_from("<H", raw, 8)[0] + struct.unpack_from("<H", raw, 10)[0] + struct.unpack_from("<I", raw, 12)[0]
        cases.append((label, path, expected, signed_end if native else None, floor))

    def signed(encoded=None, protected=None, version=(2, 3, 4, 5), counter=7, salt=32, flags=0, payload=body, key_value=key):
        if encoded is None:
            encoded = sign.canonical(metadata)
        entries = protected if protected is not None else tlv(0x50, struct.pack("<I", counter)) + tlv(0xa0, encoded)
        protected = struct.pack("<HH", 0x6908, len(entries) + 4) + entries
        header = struct.pack("<IIHHIIBBHII", sign.MAGIC, 0, settings["layout"]["header_bytes"], len(protected), len(payload), flags, *version, 0)
        raw = header + payload + protected
        digest = hashlib.sha256(raw).digest()
        signature = key_value.sign(raw, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=salt), hashes.SHA256())
        entries = tlv(0x10, digest) + tlv(0x01, hashlib.sha256(key_value.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.PKCS1)).digest()) + tlv(0x23, signature)
        return raw + struct.pack("<HH", 0x6907, len(entries) + 4) + entries

    add("offline-signer-positive", valid, "Accept", native=True)
    # Verify the actual offline producer too; malformed cases below are test-only.
    sign.verify_image(valid, public_data, pin, PROFILE, settings["configuration_sha256"], 1)
    add("stored-security-floor", valid, "Format(FormatError::SecurityFloor)", native=True, floor=8)
    old = copy.deepcopy(metadata)
    old.update(version=[0, 1, 0, 0], security_counter=8)
    old_image, _ = sign.produce(body, old, policy, key)
    add("older-version-permitted-by-security-floor", old_image, "Accept", native=True, floor=8)
    for field, value in {
        "target": "other-camera", "configuration_sha256": "1" * 64,
        "layout_sha256": "2" * 64, "policy_sha256": "3" * 64,
        "payload_sha256": "4" * 64, "role": "other-image-role", "schema": 2,
        "security_counter": 8, "version": [2, 3, 4, 6],
    }.items():
        changed = copy.deepcopy(metadata)
        changed[field] = value
        add("signed-wrong-" + field.replace("_", "-"), signed(sign.canonical(changed)), "Format(FormatError::Manifest)", native=True)
    add("signed-unknown-manifest-field", signed(sign.canonical(dict(metadata, extra=1))), "Format(FormatError::Manifest)", native=True)
    add("signed-noncanonical-manifest", signed(json.dumps(metadata, sort_keys=True).encode()), "Format(FormatError::Manifest)", native=True)
    duplicate = sign.canonical(metadata)[:-1] + b',"target":"esp32s3-camera-mcuboot"}'
    add("signed-duplicate-manifest-field", signed(duplicate), "Format(FormatError::Manifest)", native=True)
    add("signed-nested-manifest", signed(b'{"target":' + b'['*400 + b'0' + b']'*400 + b'}'), "Format(FormatError::Manifest)", native=True)
    count = tlv(0x50, struct.pack("<I", 7))
    manifest = tlv(0xa0, sign.canonical(metadata))
    add("signed-zero-counter", signed(counter=0), "Format(FormatError::Tlv)", native=True)
    add("signed-duplicate-zero-counter", signed(protected=tlv(0x50, bytes(4))+count+manifest), "Format(FormatError::Tlv)", native=True)
    add("signed-duplicate-manifest-tlv", signed(protected=count+manifest+manifest), "Format(FormatError::Tlv)", native=True)
    add("signed-unknown-protected-tlv", signed(protected=count+manifest+tlv(0x51,b'x')), "Format(FormatError::Tlv)", native=True)
    add("signed-flags", signed(flags=1), "Format(FormatError::Header)", native=True)
    add("signed-wrong-pss-salt", signed(salt=31), "Crypto")
    other = sign.rsa.generate_private_key(public_exponent=65537, key_size=3072)
    foreign = signed(key_value=other)
    add("other-root", foreign, "Format(FormatError::Key)")
    # The unauthenticated key-id TLV is not a substitute for verifying the root.
    foreign = bytearray(foreign)
    foreign[-420:-388] = key_hash
    add("other-root-with-approved-key-id", foreign, "Crypto")
    changed = bytearray(valid)
    changed[-1] ^= 1
    add("signature-corruption", changed, "Crypto")
    changed = bytearray(valid)
    changed[80] ^= 1
    add("payload-corruption", changed, "Format(FormatError::Digest)")
    add("trailing-preconfirmation", valid+bytes(16), "Format(FormatError::Tlv)")
    changed = bytearray(valid)
    struct.pack_into("<I",changed,12,0xffffffff)
    add("length-overflow", changed, "Format(FormatError::Size)")
    changed = bytearray(valid)
    struct.pack_into("<H",changed,10,0xffff)
    add("protected-length-bound", changed, "Format(FormatError::Header)")
    add("elf-payload", signed(payload=b'\x7fELF'+body), "Format(FormatError::Header)", native=True)
    add("wrapped-payload", signed(payload=struct.pack('<I',sign.MAGIC)+body), "Format(FormatError::Header)", native=True)
    layout = settings["layout"]
    trust = {"policy_sha256": bytes.fromhex(pin), "layout_sha256": hashlib.sha256(sign.canonical(layout)).digest(),
             "configuration_sha256": bytes.fromhex(settings["configuration_sha256"]), "key_sha256": key_hash}
    lines = ["const MODULUS: [u8;384] = " + rust_bytes(key.public_key().public_numbers().n.to_bytes(384, "big")) + ";",
             "fn trusted() -> Trust<'static> { Trust { target: " + json.dumps(PROFILE) + ","]
    lines.extend(name+":"+rust_bytes(value)+"," for name,value in trust.items())
    lines.extend(["layout: firmware_image::Layout {" + ",".join(f"{name}:{value}" for name,value in layout.items()) + "} } }", "static CASES: &[Case] = &["])
    for label,path,expected,signed_end,floor in cases:
        lines.append('Case { label: '+json.dumps(label)+', image: include_bytes!('+json.dumps(str(path))+'), floor:'+str(floor)+', expected: Expected::'+expected+', native_signed_end: '+('Some('+str(signed_end)+')' if signed_end else 'None')+' },')
    lines.append("];\n")
    generated = work / "cases.rs"
    generated.write_text("\n".join(lines))
    return generated, [case[0] for case in cases], len(valid)


def qualify(args):
    builder.checked_environment()
    work = target.fresh(args.work)
    logs = work / "logs"
    logs.mkdir(mode=0o700)
    names = ["Cargo.toml", "Cargo.lock", "driver/firmware-image/Cargo.toml",
             "driver/firmware-image/src/lib.rs", "tools/boot/qualify_admission.py",
             "tools/boot/build_esp32s3.py", "tools/boot/qualify.py", "tools/boot/sign.py",
             "tools/boot/target_plan.py", "tools/boot/native_inputs.py",
             "security/boot-dependencies.json", "security/esp32s3-boot-dependencies.json"]
    names.extend(str(path.relative_to(ROOT)) for path in FIXTURE.rglob("*") if path.is_file())
    inputs = {name:process.digest(ROOT/name) for name in sorted(names)}
    environment = {key:os.environ[key] for key in ("PATH","HOME","CARGO_HOME","RUSTUP_HOME","TMPDIR") if key in os.environ}
    environment.update(PYTHONNOUSERSITE="1")
    environment["PATH"] = str(Path(sys.executable).parent) + ":" + environment["PATH"]
    spec = json.loads((ROOT / "security/boot-dependencies.json").read_bytes())
    source = builder.archive(spec["mbedtls"], work, "mbedtls", args.cache, 32*1024*1024)
    compiler = shutil.which("clang", path=environment["PATH"])
    target.require(compiler is not None)
    status, compiler_version = process.run([compiler, "--version"], work, environment, logs, "compiler-version", 30)
    target.require(status == 0 and "clang" in compiler_version.lower())
    status, rust_version = process.run(["rustc", "+1.90.0", "-Vv"], ROOT, environment, logs, "rust-version", 30)
    target.require(status == 0 and "release: 1.90.0" in rust_version)
    build = work / "native"
    flags = '-DMBEDTLS_CONFIG_FILE="' + str(FIXTURE / "mbedtls_config.h") + '" -O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined'
    # CMake shell command retains the header's quotes through compilation.
    flags = flags.replace('"', '\\"')
    status, _ = process.run(["cmake","-S",str(source),"-B",str(build),"-G","Ninja",
        "-DENABLE_TESTING=OFF","-DENABLE_PROGRAMS=OFF","-DGEN_FILES=OFF", "-DCMAKE_C_COMPILER="+compiler,
        "-DCMAKE_C_FLAGS="+flags], work,environment,logs,"native-configure",120)
    target.require(status == 0)
    status, _ = process.run(["cmake","--build",str(build),"--target","mbedcrypto","-j2"],work,environment,logs,"native-build",300)
    target.require(status == 0)
    status, _ = process.run([compiler,"-std=c11",*shlex.split(flags),"-I"+str(source/"include"),"-c",str(FIXTURE/"abi.c"),"-o",str(build/"abi.o")],work,environment,logs,"native-abi",60)
    target.require(status == 0)
    status, _ = process.run(["ar","rcs",str(build/"library/libnxrs_mbedtls_abi.a"),str(build/"abi.o")],work,environment,logs,"native-archive",60)
    target.require(status == 0)
    status, definitions = process.run([compiler,*shlex.split(flags),"-I"+str(source/"include"),
        "-dM","-E","-include","mbedtls/build_info.h","-x","c","/dev/null"],work,environment,logs,"native-definitions",60)
    target.require(status == 0)
    values = native_inputs.defines(definitions)
    modules = {name for name in values if name.startswith("MBEDTLS_") and name.endswith("_C")}
    target.require(modules == CRYPTO_MODULES)
    target.require("MBEDTLS_PKCS1_V21" in values and "MBEDTLS_PKCS1_V15" not in values)
    generated, labels, prefixes = fixtures(work)
    environment.update(NXRS_ADMISSION_NATIVE_LIB=str(build/"library"),NXRS_ADMISSION_CASES=str(generated),
        CARGO_TARGET_DIR=str(work/"rust"),RUSTFLAGS="-C default-linker-libraries=yes -C linker="+compiler+" -C link-arg=-fsanitize=address,undefined",
        ASAN_OPTIONS="halt_on_error=1:detect_leaks="+("0" if platform.system()=="Darwin" else "1"),UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=0")
    for name, arguments, diagnostic, disable in (
        ("address", ["overflow"], "AddressSanitizer: heap-buffer-overflow", "undefined"),
        ("undefined", [], "runtime error: shift exponent 32", "address"),
    ):
        # Isolate each runtime so the other sanitizer cannot intercept its probe.
        probe = work / ("sanitizer-probe-" + name)
        status, _ = process.run([compiler, *shlex.split(flags), "-fno-sanitize=" + disable,
            str(FIXTURE / "sanitizer_probe.c"), "-o", str(probe)], work, environment, logs,
            "sanitizer-probe-build-" + name, 60)
        target.require(status == 0)
        status, _ = process.run([str(probe), *arguments], work, environment, logs,
            "sanitizer-negative-" + name, 30)
        target.require(status != 0 and diagnostic in
                       (logs / ("sanitizer-negative-" + name + ".err")).read_text())
    status, _ = process.run(["cargo","+1.90.0","build","--locked","-p","nxrs-image-admission-qualification","--features","qualification"],ROOT,environment,logs,"rust-build",300)
    target.require(status == 0)
    executable = work/"rust/debug/nxrs-image-admission-qualification"
    status, output = process.run([str(executable)],work,environment,logs,"native-execution",180)
    target.require(status == 0)
    status, refusal = process.run([str(executable), "floor-refusal"], work, environment, logs, "native-execution-floor-refusal", 60)
    target.require(status == 7 and refusal == "IMAGE_ADMISSION_EXPECTED_REFUSAL floor\n")
    target.require(all(output.count("IMAGE_ADMISSION_CASE "+label+" passed\n")==1 for label in labels))
    target.require("IMAGE_ADMISSION_QUALIFIED cases="+str(len(labels))+" proper_prefixes="+str(prefixes)+" corruptions=10000\n" in output)
    target.require(all(process.digest(ROOT/name) == digest for name,digest in inputs.items()))
    status, revision = process.run(["git","rev-parse","HEAD"], ROOT, environment, logs, "repository-head", 30)
    target.require(status == 0 and re.fullmatch(r"[0-9a-f]{40}\n", revision) is not None)
    status, changes = process.run(["git","status","--porcelain=v1","--untracked-files=normal"], ROOT, environment, logs, "repository-changes", 30)
    target.require(status == 0)
    report = {
        "schema": 1,
        "scope": "host-rust-image-admission-real-native-crypto",
        "qualified": True,
        "repository_head": revision.strip(),
        "repository_has_changes": bool(changes),
        "inputs": inputs,
        "compiler": compiler_version.splitlines()[0],
        "compiler_executable_sha256": process.digest(Path(compiler).resolve()),
        "rustc": rust_version.strip(),
        "operator_runtime": native_inputs.operator_runtime(),
        "native_crypto_modules": sorted(modules),
        "native_flags": flags.replace(str(ROOT), "@nxrs"),
        "native_archive_sha256": process.digest(build / "library/libmbedcrypto.a"),
        "abi_archive_sha256": process.digest(build / "library/libnxrs_mbedtls_abi.a"),
        "source": spec["mbedtls"],
        "configuration_sha256": process.digest(FIXTURE / "mbedtls_config.h"),
        "required_cases": labels,
        "proper_prefixes_refused": prefixes,
        "corruptions_refused": 10000,
        "actual_nonzero_process_refusal": True,
        "executable_sha256": process.digest(executable),
        "native_c_address_undefined_sanitizers": True,
        "rust_sanitizer_instrumentation": False,
        "actual_sanitizer_negative_controls": ["heap-buffer-overflow", "undefined-shift"],
        "native_leak_detection": platform.system() != "Darwin",
        "nuttx_abi_qualified": False,
        "physical_qualified": False,
        "native_advisory_closure": False,
        "protected_device_counter": False,
        "activation_qualified": False,
        "deployment_permitted": False,
    }
    (work / "admission-qualification.json").write_bytes(target.canonical(report) + b"\n")
    print("Rust image admission qualified against actual frozen native crypto; device gates remain open")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work",type=Path,required=True)
    parser.add_argument("--cache",type=Path)
    args = parser.parse_args()
    try:
        qualify(args)
    except Exception as error:
        import traceback
        frames=[f for f in traceback.extract_tb(error.__traceback__) if Path(f.filename).parent.resolve()==Path(__file__).parent.resolve()]
        if frames:
            f=frames[-2] if len(frames)>1 and frames[-1].name=="require" else frames[-1]
            print("Image admission qualification refused at "+Path(f.filename).name+":"+str(f.lineno),file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
