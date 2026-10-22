#!/usr/bin/env python3
"""Build-only ESP32-S3 MCUboot wiring for an independently pinned public policy."""
import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import stat
import struct
import sys
import tarfile
import time
import urllib.request

import qualify as process
import sign
import target_plan as policy
import native_inputs
import evidence
import upstream_advisories as upstream

ROOT = Path(__file__).resolve().parents[2]


def checked_environment():
    spec = json.loads((ROOT / "security/esp32s3-boot-dependencies.json").read_text())
    policy.require(sys.version_info >= (3, 12) and sys.byteorder == "little")
    for name, expected in spec["operator_versions"].items():
        policy.require(version(name) == expected)
    for name, expected in spec["files"].items():
        process.verify(ROOT / name, expected)
    return spec


def archive(entry, work, name, cache, budget):
    destination = work / (name + ".archive")
    cached = cache / (entry["sha256"] + ".archive") if cache else None
    source = None
    try:
        if cached is not None and cached.exists():
            fd = os.open(cached, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(fd)
            source = os.fdopen(fd, "rb")
            policy.require(stat.S_ISREG(info.st_mode) and info.st_size <= budget)
        else:
            request = urllib.request.Request(entry["url"], headers={"User-Agent": "nxrs-boot-build"})
            source = urllib.request.urlopen(request, timeout=60)
        total = 0
        deadline = time.monotonic() + 300
        with destination.open("xb") as output:
            while block := source.read(1024 * 1024):
                total += len(block)
                policy.require(total <= budget and time.monotonic() <= deadline)
                output.write(block)
    finally:
        if source is not None:
            source.close()
    # Always authenticate an independent private copy; never extract a mutable cache.
    process.verify(destination, entry["sha256"])
    folder = work / (name + "-source")
    folder.mkdir(mode=0o700)
    with tarfile.open(destination) as packed:
        members = packed.getmembers()
        policy.require(len(members) <= 50000 and sum(x.size for x in members) <= 2 * 1024 * 1024 * 1024)
        packed.extractall(folder, filter="data")
    roots = list(folder.iterdir())
    policy.require(len(roots) == 1 and roots[0].is_dir())
    return roots[0]


def compiler_command(entry):
    tokens = shlex.split(entry["command"])
    result = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("-o", "-MF", "-MT", "-MQ"):
            index += 2
            continue
        if token in ("-c", "-MD", "-MMD", "-MP") or token == entry["file"]:
            index += 1
            continue
        policy.require(not token.startswith("@"))
        result.append(token)
        index += 1
    return result


def check_defines(text):
    values = {}
    for line in text.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) >= 2 and fields[0] == "#define":
            values[fields[1]] = fields[2] if len(fields) == 3 else ""
    expected = {"MCUBOOT_SIGN_RSA": "", "MCUBOOT_SIGN_RSA_LEN": "CONFIG_ESP_SIGN_RSA_LEN",
        "CONFIG_ESP_SIGN_RSA_LEN": "3072", "MCUBOOT_USE_MBED_TLS": "",
        "MCUBOOT_VALIDATE_PRIMARY_SLOT": "", "MCUBOOT_MAX_IMG_SECTORS": "512",
        "MCUBOOT_IMAGE_NUMBER": "1", "MCUBOOT_USE_TLV_ALLOW_LIST": "1",
        "MCUBOOT_SWAP_USING_SCRATCH": "1", "MCUBOOT_HAVE_ASSERT_H": "1",
        "MCUBOOT_FIH_PROFILE_OFF": "",
        "MCUBOOT_DOWNGRADE_PREVENTION": "1",
        "MCUBOOT_DOWNGRADE_PREVENTION_SECURITY_COUNTER": "1", "BOOT_MAX_ALIGN": "8"}
    policy.require(all(values.get(name) == value for name, value in expected.items()))
    absent = ("CONFIG_SECURE_BOOT", "CONFIG_SECURE_FLASH_ENC_ENABLED", "CONFIG_EFUSE_VIRTUAL",
              "MCUBOOT_HW_ROLLBACK_PROT", "MCUBOOT_USE_TINYCRYPT", "MCUBOOT_OVERWRITE_ONLY",
              "MCUBOOT_DIRECT_XIP", "MCUBOOT_RAM_LOAD", "MCUBOOT_SIGN_EC256", "MCUBOOT_HW_KEY", "MCUBOOT_BUILTIN_KEY", "MCUBOOT_ENC_IMAGES", "NDEBUG")
    policy.require(not any(name in values for name in absent))
    return {"required": expected, "required_absent": list(absent)}


def wrappers(work, public, der):
    runtime = str(Path(sys.executable).absolute())
    policy.require(re.fullmatch(r"/[A-Za-z0-9_./-]+", runtime))
    folder = work / "sdk-bin"
    folder.mkdir(mode=0o700)
    elf = str(work / "build/mcuboot_esp32s3.elf")
    image = str(work / "build/mcuboot_esp32s3.bin")
    paths = {}
    for kind in ("imgtool", "esptool"):
        path = folder / kind
        text = "#!" + runtime + "\nimport sys\nsys.path.insert(0, " + repr(str(ROOT / "tools/boot")) + ")\n"
        text += "from sdk_cli import main\nmain(" + ", ".join(map(repr,
            (kind, str(public), hashlib.sha256(der).hexdigest(), elf, image))) + ", sys.argv[1:])\n"
        with path.open("x") as output:
            output.write(text)
        path.chmod(0o700)
        paths[kind] = path
    return paths


def symbols(text):
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 6 and fields[1:3] == ["g", "O"]:
            name = fields[5]
            if name in ("rsa_pub_key", "rsa_pub_key_len", "bootutil_keys", "bootutil_key_cnt"):
                policy.require(name not in result)
                result[name] = {"address": int(fields[0], 16), "size": int(fields[4], 16)}
    policy.require(set(result) == {"rsa_pub_key", "rsa_pub_key_len", "bootutil_keys", "bootutil_key_cnt"})
    return result


def verify_key(elf_path, table, layout, expected):
    from esptool.bin_image import ELFFile
    policy.require(elf_path.stat().st_size <= 4 * 1024 * 1024)
    raw = elf_path.read_bytes()
    policy.require(raw[:6] == b"\x7fELF\x01\x01" and struct.unpack_from("<H", raw, 18)[0] == 94)
    elf = ELFFile(raw)

    def read(address, size):
        candidates = [s for s in elf.sections if s.addr <= address and address + size <= s.addr + len(s.data)]
        policy.require(len(candidates) == 1)
        section = candidates[0]
        return section.data[address - section.addr:address - section.addr + size]

    size, key_offset, length_offset, pointer_size, count_size = layout
    policy.require(pointer_size == count_size == 4 and size <= 64
                   and key_offset + 4 <= size and length_offset + 4 <= size)
    policy.require(table["rsa_pub_key"]["size"] == len(expected)
                   and read(table["rsa_pub_key"]["address"], len(expected)) == expected)
    policy.require(table["bootutil_key_cnt"]["size"] == 4
                   and read(table["bootutil_key_cnt"]["address"], 4) == struct.pack("<I", 1))
    policy.require(table["rsa_pub_key_len"]["size"] == 4
                   and read(table["rsa_pub_key_len"]["address"], 4) == struct.pack("<I", len(expected)))
    policy.require(table["bootutil_keys"]["size"] == size)
    actual = read(table["bootutil_keys"]["address"], size)
    policy.require(struct.unpack_from("<I", actual, key_offset)[0] == table["rsa_pub_key"]["address"]
                   and struct.unpack_from("<I", actual, length_offset)[0] == table["rsa_pub_key_len"]["address"])


def build(args):
    from cryptography.hazmat.primitives import serialization
    spec = checked_environment()
    data, public_policy, public = policy.checked_policy(args.platform, args.policy, args.policy_sha256)
    host = platform.system() + "-" + platform.machine()
    policy.require(host in spec["compiler"]["hosts"])
    work = policy.fresh(args.out)
    for name in ("logs", "home", "inputs"):
        (work / name).mkdir(mode=0o700)
    inputs = {name: process.digest(ROOT / name) for name in (
        "tools/boot/build_esp32s3.py", "tools/boot/target_plan.py", "tools/boot/sdk_cli.py",
        "tests/boot/key_layout.c", "tools/boot/native_inputs.py", "tools/boot/evidence.py",
        "tools/boot/upstream_advisories.py", "security/boot-advisory-review.json",
        "security/esp32s3-boot-dependencies.json",
        "security/boot-dependencies.json", *spec["files"])}
    base = json.loads((ROOT / "security/boot-dependencies.json").read_text())
    source = archive(dict(base["mcuboot"], sha256=base["mcuboot"]["sha256"]), work, "mcuboot", args.cache, 32*1024*1024)
    mbed = archive(base["mbedtls"], work, "mbedtls", args.cache, 32*1024*1024)
    hal = archive(spec["hal"], work, "hal", args.cache, 64*1024*1024)
    compiler = archive(spec["compiler"]["hosts"][host], work, "compiler", args.cache, 256*1024*1024)
    (source / "ext/mbedtls").rmdir()
    shutil.copytree(mbed, source / "ext/mbedtls")
    with sign.directory(work / "inputs") as fd:
        sign.write_new(fd, "boot-policy.json", public_policy)
    public_path = work / "inputs/application-public.pem"
    sign.export_public(public_policy, args.policy_sha256, args.platform,
                       data["settings"]["configuration_sha256"], public_path)
    der = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.PKCS1)
    sdk = wrappers(work, public_path, der)
    environment = {"PATH": str(compiler / "bin") + ":" + str(Path(sys.executable).parent) + ":/usr/bin:/bin:/usr/sbin:/sbin",
                   "HOME": str(work / "home"), "PYTHONNOUSERSITE": "1", "SOURCE_DATE_EPOCH": "0"}
    logs = work / "logs"
    for number, name in enumerate(sorted(spec["files"])):
        if name.endswith(".patch"):
            status, _ = process.run(["/usr/bin/patch", "--batch", "--fuzz=0", "-p1", "-i", str(ROOT / name)],
                                    source, environment, logs, "patch-" + str(number), 30)
            policy.require(status == 0)
    configuration = work / "bootloader.conf"
    configuration.write_text(policy.bootloader_config(data, public_path))
    cmake = str(Path(sys.executable).parent / "cmake")
    flags = "-Os -g -ffile-prefix-map=" + str(work) + "=/nxrs-build"
    command = [cmake, "-S", str(source / "boot/espressif"), "-B", str(work / "build"), "-G", "Ninja",
        "-DMCUBOOT_TARGET=esp32s3", "-DMCUBOOT_CONFIG_FILE=" + str(configuration), "-DESP_HAL_PATH=" + str(hal),
        "-DCONFIG_ESP_FLASH_SIZE=16MB", "-DESP_FLASH_MODE=dio", "-DESP_FLASH_FREQ=40m",
        "-DIMGTOOL_COMMAND=" + str(sdk["imgtool"]), "-DESPTOOL_COMMAND=" + str(sdk["esptool"]),
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON", "-DCMAKE_BUILD_TYPE=Release",
        "-DMBEDTLS_CONFIG_FILE=" + str(ROOT / "tools/boot/mbedtls_config.h"),
        "-DCMAKE_C_FLAGS_RELEASE=" + flags, "-DCMAKE_CXX_FLAGS_RELEASE=" + flags,
        "-DCMAKE_ASM_FLAGS_RELEASE=" + flags,
        "-DNXRS_MCUBOOT_VERSION=" + base["mcuboot"]["version"] + "+" + base["mcuboot"]["commit"]]
    status, _ = process.run(command, work, environment, logs, "configure", 180)
    policy.require(status == 0)
    status, _ = process.run([cmake, "--build", str(work / "build"), "--target", "mcuboot_esp32s3.elf", "--parallel", "2"],
                            work, environment, logs, "build", 300)
    policy.require(status == 0)
    entries = json.loads((work / "build/compile_commands.json").read_text())
    entry = [x for x in entries if x["file"].endswith("/image_validate.c")]
    policy.require(len(entry) == 1)
    entry = entry[0]
    arguments = compiler_command(entry)
    policy.require(Path(arguments[0]).name == "xtensa-esp32s3-elf-gcc"
                   and os.path.samefile(arguments[0], compiler / "bin/xtensa-esp32s3-elf-gcc"))
    status, output = process.run([*arguments, "-dM", "-E", entry["file"]], Path(entry["directory"]), environment, logs, "defines", 60)
    policy.require(status == 0)
    defines = check_defines(output)
    probe = ROOT / "tests/boot/key_layout.c"
    status, _ = process.run([*arguments, "-fdata-sections", "-c", str(probe), "-o", str(work / "key-layout.o")],
                            work, environment, logs, "key-layout", 60)
    policy.require(status == 0)
    objcopy = str(compiler / "bin/xtensa-esp32s3-elf-objcopy")
    status, _ = process.run([objcopy, "--only-section", ".rodata.nxrs_key_layout", "-O", "binary",
                            str(work / "key-layout.o"), str(work / "key-layout.bin")], work, environment, logs, "key-layout-data", 30)
    policy.require(status == 0 and (work / "key-layout.bin").stat().st_size == 20)
    layout = struct.unpack("<5I", (work / "key-layout.bin").read_bytes())
    elf = work / "build/mcuboot_esp32s3.elf"
    status, output = process.run([str(compiler / "bin/xtensa-esp32s3-elf-objdump"), "-t", str(elf)],
                                 work, environment, logs, "symbols", 30)
    policy.require(status == 0)
    verify_key(elf, symbols(output), layout, der)
    binary = work / "build/mcuboot_esp32s3.bin"
    policy.require(binary.is_file() and 0 < binary.stat().st_size <= data["configuration"]["boot"]["bootloader_bytes"])
    status, compiler_version = process.run([str(compiler / "bin/xtensa-esp32s3-elf-gcc"), "--version"],
                                          work, environment, logs, "compiler-version", 30)
    policy.require(status == 0 and "esp-14.2.0_20241119" in compiler_version)
    policy.require(all(process.digest(ROOT / name) == digest for name, digest in inputs.items()))
    policy.require(policy.plan(args.platform) == data)
    status, revision = process.run(["git", "rev-parse", "HEAD"], ROOT, environment, logs, "repository-head", 30)
    policy.require(status == 0 and re.fullmatch(r"[0-9a-f]{40}\n?", revision))
    status, changes = process.run(["git", "status", "--porcelain=v1", "--untracked-files=normal"],
                                 ROOT, environment, logs, "repository-state", 30)
    policy.require(status == 0)
    report = {"schema": 1, "scope": "esp32s3-cross-build-only", "platform": args.platform,
        "repository_head": revision.strip(), "repository_has_changes": bool(changes.strip()),
        "public_policy_sha256": args.policy_sha256, "public_key_pkcs1_sha256": policy.digest(der),
        "configuration_sha256": data["settings"]["configuration_sha256"],
        "sources": data["sources"], "profile_sources": data["profile_sources"],
        "compiler_archive": spec["compiler"]["hosts"][host], "compiler": compiler_version.splitlines()[0],
        "operator_versions": {name: version(name) for name in spec["operator_versions"]},
        "operator_runtime": native_inputs.operator_runtime(),
        "effective_defines": defines, "target_key_layout": list(layout),
        "inputs": inputs,
        "bootloader_configuration_sha256": process.digest(configuration),
        "release_compiler_flags": flags.replace(str(work), "/private-build"),
        "assertions_enabled": True, "fault_injection_hardening": "off",
        "elf_sha256": process.digest(elf), "binary_sha256": process.digest(binary),
        "binary_bytes": binary.stat().st_size, "cross_build_qualified": True,
        "rom_authenticated": False, "protected_device_counter": False, "physical_qualified": False,
        "native_advisory_closure": False, "deployment_permitted": False}
    inventory = native_inputs.collect(work, source, hal, compiler, environment, report)
    capture_path = work / "official-advisory-capture.json"
    status, _ = process.run([sys.executable, str(ROOT / "tools/boot/upstream_advisories.py"),
        "--out", str(capture_path)], work, environment, logs, "upstream-advisories", 300)
    if status != 0:
        # Relay only the collector's bounded, fixed-field diagnostic protocol.
        error = (logs / "upstream-advisories.err").read_text()
        diagnostic = upstream.public_diagnostic(error)
        if diagnostic:
            print(diagnostic, file=sys.stderr)
    policy.require(status == 0)
    captured = json.loads(native_inputs.bounded(capture_path))
    documents = evidence.schema_documents(work, spec)
    bindings = evidence.write(work, report, inventory, captured, documents)
    policy.require(evidence.verify(work, report, source, hal, compiler, environment, captured, documents) == bindings)
    report["native_evidence"] = bindings
    policy.require(all(process.digest(ROOT / name) == digest for name, digest in inputs.items()))
    (work / "boot-build.json").write_bytes(policy.canonical(report) + b"\n")
    print("ESP32-S3 sole-key bootloader cross-build and actual target-header binding checks passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "verify-policy", "build", "record-payload"):
        command = sub.add_parser(name)
        command.add_argument("--platform", required=True)
        if name != "verify-policy":
            command.add_argument("--out", required=True, type=Path)
        if name != "plan":
            command.add_argument("--policy", required=True, type=Path)
            command.add_argument("--policy-sha256", required=True)
        if name == "build":
            command.add_argument("--cache", type=Path)
        if name == "record-payload":
            command.add_argument("--config", required=True, type=Path)
            command.add_argument("--elf", required=True, type=Path)
            command.add_argument("--hex", required=True, type=Path)
    args = parser.parse_args()
    try:
        checked_environment()
        if args.command == "plan":
            policy.emit(args.platform, args.out)
            print("Boot plan and offline signer settings emitted")
        elif args.command == "verify-policy":
            policy.checked_policy(args.platform, args.policy, args.policy_sha256)
            print("Product boot public policy verified")
        elif args.command == "record-payload":
            policy.record_payload(args.platform, args.policy, args.policy_sha256,
                                  args.config, args.elf, args.hex, args.out)
            print("Exact unsigned payload and policy-bound build evidence emitted")
        else:
            build(args)
    except Exception as error:
        import traceback
        frames = [frame for frame in traceback.extract_tb(error.__traceback__)
                  if Path(frame.filename).parent.resolve() == Path(__file__).parent.resolve()]
        if frames:
            frame = frames[-2] if len(frames) > 1 and frames[-1].name == "require" else frames[-1]
            print("Boot build operation failed at " + Path(frame.filename).name + ":" + str(frame.lineno), file=sys.stderr)
        else:
            print("Boot build operation failed; no device operation is available", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
