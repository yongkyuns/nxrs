#!/usr/bin/env python3
"""Compile actual Xtensa admission crypto/ABI; optionally link and run NuttX fixtures."""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import shlex
import struct
import sys

import build_esp32s3 as builder
import native_inputs
import qualify as process
import qualify_admission as admission
import target_plan as target

ROOT = target.ROOT
FIXTURE = admission.FIXTURE


def build_native(work, logs, environment, cache):
    spec = builder.checked_environment()
    base = json.loads((ROOT / "security/boot-dependencies.json").read_bytes())
    host = platform.system() + "-" + platform.machine()
    target.require(host in spec["compiler"]["hosts"])
    compiler_spec = spec["compiler"]["hosts"][host]
    source = builder.archive(base["mbedtls"], work, "mbedtls", cache, 32*1024*1024)
    compiler = builder.archive(compiler_spec, work, "compiler", cache, 256*1024*1024)
    gcc = compiler / "bin/xtensa-esp32s3-elf-gcc"
    build = work / "native"
    flags = '-DMBEDTLS_CONFIG_FILE=\\"' + str(FIXTURE / "mbedtls_config.h") + '\\" -Os -g -fno-lto -mlongcalls -mtext-section-literals -ffunction-sections -fdata-sections'
    status, version = process.run([str(gcc), "--version"], work, environment, logs, "compiler-version", 30)
    target.require(status == 0 and "esp-14.2.0_20241119" in version)
    status, _ = process.run(["cmake", "-S", str(source), "-B", str(build), "-G", "Ninja",
        "-DCMAKE_SYSTEM_NAME=Generic", "-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY",
        "-DCMAKE_C_COMPILER=" + str(gcc), "-DCMAKE_C_FLAGS=" + flags,
        "-DENABLE_TESTING=OFF", "-DENABLE_PROGRAMS=OFF", "-DGEN_FILES=OFF"],
        work, environment, logs, "target-configure", 120)
    target.require(status == 0)
    status, _ = process.run(["cmake", "--build", str(build), "--target", "mbedcrypto", "-j2"],
        work, environment, logs, "target-build", 300)
    target.require(status == 0)
    arguments = [str(gcc), *shlex.split(flags), "-I" + str(source / "include")]
    status, definitions = process.run([*arguments, "-dM", "-E", "-include", "mbedtls/build_info.h", "-x", "c", "/dev/null"],
        work, environment, logs, "target-definitions", 60)
    target.require(status == 0)
    values = native_inputs.defines(definitions)
    modules = {name for name in values if name.startswith("MBEDTLS_") and name.endswith("_C")}
    target.require(modules == admission.CRYPTO_MODULES and values.get("MBEDTLS_VERSION_STRING") == '"3.6.7"'
                   and "MBEDTLS_PKCS1_V21" in values and "MBEDTLS_PKCS1_V15" not in values
                   and values.get("__XTENSA_WINDOWED_ABI__") == "1" and values.get("__SIZEOF_POINTER__") == "4"
                   and values.get("__BYTE_ORDER__") == "__ORDER_LITTLE_ENDIAN__")
    abi = build / "abi.o"
    status, _ = process.run([*arguments, "-std=c11", "-DNXRS_ADMISSION_ABI_LAYOUT_WITNESS=1", "-c", str(FIXTURE / "abi.c"), "-o", str(abi)],
        work, environment, logs, "target-abi", 60)
    target.require(status == 0)
    status, header = process.run([str(compiler / "bin/xtensa-esp32s3-elf-readelf"), "-h", str(abi)],
        work, environment, logs, "target-abi-header", 30)
    target.require(status == 0 and "ELF32" in header and "little endian" in header and "Tensilica Xtensa Processor" in header)
    status, _ = process.run([str(compiler / "bin/xtensa-esp32s3-elf-objcopy"),
        "--dump-section", ".nxrs_admission_abi=" + str(build / "abi-layout.bin"), str(abi)],
        work, environment, logs, "target-abi-layout", 30)
    target.require(status == 0)
    raw = native_inputs.bounded(build / "abi-layout.bin", 16)
    target.require(len(raw) == 16)
    size, align, sha256_id, pointer_bytes = struct.unpack("<4I", raw)
    target.require(0 < size <= 65536 and 0 < align <= 4096 and align & (align-1) == 0
                   and size % align == 0 and 0 < sha256_id < 256 and pointer_bytes == 4)
    library = build / "library"
    status, _ = process.run([str(compiler / "bin/xtensa-esp32s3-elf-ar"), "rcs",
        str(library / "libnxrs_mbedtls_abi.a"), str(abi)], work, environment, logs, "target-abi-archive", 30)
    target.require(status == 0)
    return {"source": base["mbedtls"], "compiler_archive": compiler_spec,
        "compiler": version.splitlines()[0], "configuration_sha256": process.digest(FIXTURE / "mbedtls_config.h"),
        "crypto_modules": sorted(modules), "flags": flags.replace(str(ROOT), "@nxrs"),
        "native_archive_sha256": process.digest(library / "libmbedcrypto.a"),
        "abi_archive_sha256": process.digest(library / "libnxrs_mbedtls_abi.a"),
        "target_header_abi": {"rsa_context_bytes": size, "rsa_context_align": align,
                              "sha256_enum": sha256_id, "pointer_bytes": pointer_bytes}}, library, compiler


def verify_execution(text, status, mode, labels, prefixes):
    lines = text.replace("\r", "").splitlines()
    target.require(not any(word in text for word in ("PANIC", "panic:", "panicked at", "Assertion failed")))
    values = re.findall(r"^IMAGE_ADMISSION_STATUS_([0-9]+)\s*$", status.replace("\r", ""), re.M)
    if mode == "pass":
        actual = [line for line in lines if line.startswith("IMAGE_ADMISSION_CASE ")]
        target.require(actual == ["IMAGE_ADMISSION_CASE " + label + " passed" for label in labels])
        expected = "IMAGE_ADMISSION_QUALIFIED cases=" + str(len(labels)) + " proper_prefixes=" + str(prefixes) + " corruptions=10000"
        target.require([line for line in lines if line.startswith("IMAGE_ADMISSION_QUALIFIED")] == [expected])
        target.require("IMAGE_ADMISSION_EXPECTED_REFUSAL floor" not in lines and values == ["0"])
    else:
        target.require(mode == "floor-refusal" and values == ["1"])
        target.require(lines.count("IMAGE_ADMISSION_EXPECTED_REFUSAL floor") == 1)
        target.require(not any(line.startswith(("IMAGE_ADMISSION_CASE ", "IMAGE_ADMISSION_QUALIFIED")) for line in lines))


def run_emulator(image, qemu, environment, logs, labels, prefixes):
    import select
    import signal
    import subprocess
    import time
    sys.path.insert(0, str(ROOT / "tests"))
    from rtos_harness.nuttx import verify_identity

    original = process.digest(image)
    results = []
    for mode in ("pass", "floor-refusal"):
        command = [str(qemu), "-machine", "esp32s3", "-smp", "2", "-display", "none",
            "-serial", "stdio", "-monitor", "none", "-nic", "none", "-no-reboot", "-snapshot",
            "-drive", "file=" + str(image) + ",if=mtd,format=raw"]
        child = subprocess.Popen(command, cwd=image.parent, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        transcript = bytearray()
        deadline = time.monotonic() + 600

        def read_prompt():
            start = len(transcript)
            while time.monotonic() < deadline:
                target.require(child.poll() is None)
                ready, _, _ = select.select([child.stdout], [], [], 0.1)
                if ready:
                    block = os.read(child.stdout.fileno(), 65536)
                    target.require(block and len(transcript) + len(block) <= 2 * 1024 * 1024)
                    transcript.extend(block)
                    if transcript[start:].endswith((b"nsh> ", b"nsh> \x1b[K")):
                        return transcript[start:].decode(errors="replace")
            raise TimeoutError("private emulator console deadline")

        def send(command):
            target.require(command in ("uname", "image_admission", "image_admission floor-refusal", "echo IMAGE_ADMISSION_STATUS_$?"))
            child.stdin.write((command + "\n").encode("ascii"))
            child.stdin.flush()
            return read_prompt()

        try:
            read_prompt()
            verify_identity(send)
            text = send("image_admission" + (" floor-refusal" if mode == "floor-refusal" else ""))
            status = send("echo IMAGE_ADMISSION_STATUS_$?")
            verify_execution(text, status, mode, labels, prefixes)
            target.require(process.digest(image) == original)
            results.append({"mode": mode, "success": True, "image_sha256": original,
                            "transcript_sha256": target.digest(transcript)})
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=10)
            child.stdin.close()
            child.stdout.close()
            with (logs / ("qemu-" + mode + ".log")).open("xb") as stream:
                stream.write(transcript)
    return results


def link_nuttx(work, logs, environment, library, compiler):
    import sign
    target.require(platform.system() == "Linux" and platform.machine() == "x86_64")
    generated, labels, prefixes = admission.fixtures(work)
    policy_path = work / "authority/policy.json"
    pin = process.digest(policy_path)
    environment = dict(environment, NXRS_ADMISSION_NATIVE_LIB=str(library), NXRS_ADMISSION_CASES=str(generated))
    images = []
    for name in ("esp32s3-qemu-mock", admission.PROFILE):
        output = work / ("application-" + name)
        command = ["bash", str(ROOT / "tools/build-nuttx-std-app.sh"),
            "--app-manifest", str(FIXTURE / "Cargo.toml"), "--app-package", "nxrs-image-admission-qualification",
            "--bin", "nxrs-image-admission-qualification", "--command", "image_admission",
            "--priority", "100", "--stack-size", "32768", "--platform", name,
            "--out", str(output), "--feature", "qualification"]
        if name == admission.PROFILE:
            command.extend(("--boot-policy", str(policy_path), "--boot-policy-sha256", pin))
        status, _ = process.run(command, ROOT, environment, logs, "nuttx-" + name, 1200)
        target.require(status == 0)
        elf = output / "nuttx/nuttx"
        status, symbols = process.run([str(compiler / "bin/xtensa-esp32s3-elf-nm"), "--defined-only", str(elf)],
            work, environment, logs, "nuttx-symbols-" + name, 30)
        required = ("mbedtls_rsa_rsassa_pss_verify_ext", "mbedtls_sha256", "nxrs_admission_rsa_size",
                    "nxrs_admission_rsa_align", "nxrs_admission_sha256_id", "image_admission_main")
        target.require(status == 0 and all(re.search(r" [TtWw] " + symbol + r"$", symbols, re.M) for symbol in required))
        abi = json.loads((output / "abi-report.json").read_bytes())
        std_source = json.loads((output / "std-source.json").read_bytes())
        target.require(abi["probe_abi_compatible"] and abi["target"] == "xtensa-esp32s3-nuttx"
                       and std_source["selected_source_matches"])
        item = {"platform": name, "elf_sha256": process.digest(elf), "sdk_symbols_present": list(required),
                "provider_selection_sha256": process.digest(output / "provider-selection.json"),
                "nuttx_abi_probe_compatible": True, "raw_poll_abi_compatible": abi["poll_abi_compatible"],
                "nuttx_abi_report_sha256": process.digest(output / "abi-report.json"),
                "std_source_proof_sha256": process.digest(output / "std-source.json"),
                "native_configuration_sha256": process.digest(output / "resolved.config")}
        if name == admission.PROFILE:
            payload = json.loads((output / "payload-build.json").read_bytes())
            target.require(payload["policy_sha256"] == pin and payload["configuration_sha256"] == target.plan(name)["settings"]["configuration_sha256"]
                           and payload["payload_sha256"] == process.digest(output / "payload.bin"))
            item.update(payload_sha256=payload["payload_sha256"], payload_bytes=(output / "payload.bin").stat().st_size,
                        boot_policy_sha256=pin, configuration_sha256=payload["configuration_sha256"])
        images.append(item)
    qemu_images = list((work / "application-esp32s3-qemu-mock/nuttx").glob("nuttx.merged.bin"))
    qemus = list((ROOT / "target/qemu-tools/qemu").glob("**/bin/qemu-system-xtensa"))
    target.require(len(qemu_images) == len(qemus) == 1)
    # The existing isolated SDK supplies the emulator's runtime libraries; no host networking.
    sdk_script = ROOT / "target/qemu-tools/environment.sh"
    status, exported = process.run(["bash", "-euc", 'source "$1"; command -v qemu-system-xtensa',
        "admission-qemu", str(sdk_script)], ROOT, environment, logs, "qemu-selection", 30)
    target.require(status == 0 and Path(exported.strip()).resolve() == qemus[0].resolve())
    execution = run_emulator(qemu_images[0], qemus[0], environment, logs, labels, prefixes)
    status, qemu_version = process.run([str(qemus[0]), "--version"], work, environment, logs, "qemu-version", 30)
    target.require(status == 0 and "9.0.0" in qemu_version)
    return images, {"cases": execution, "emulator_sha256": process.digest(qemus[0]),
                    "emulator_version": qemu_version.splitlines()[0]}


def nuttx_host_preflight():
    target.require(platform.system() == "Linux" and platform.machine() == "x86_64")
    # Ordinary NuttX make rules may publish to a writable TFTP directory.
    # Reject that live-service side effect before creating either fixture image.
    target.require(not (Path("/tftpboot").is_dir() and os.access("/tftpboot", os.W_OK)))


def qualify(args):
    builder.checked_environment()
    if args.nuttx:
        nuttx_host_preflight()
    work = target.fresh(args.work)
    logs = work / "logs"
    logs.mkdir(mode=0o700)
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "CARGO_HOME", "RUSTUP_HOME", "TMPDIR") if key in os.environ}
    environment.update(PYTHONNOUSERSITE="1", NXRS_BOOT_PYTHON=sys.executable)
    environment["PATH"] = str(Path(sys.executable).parent) + ":" + environment["PATH"]
    names = ["Cargo.toml", "Cargo.lock", "driver/firmware-image/Cargo.toml", "driver/firmware-image/src/lib.rs",
             "tools/boot/qualify_admission_target.py", "tools/boot/qualify_admission.py", "tools/boot/build_esp32s3.py",
             "tools/build-nuttx-std-app.sh", "tools/check-deployment.py", "tools/platform_config.py",
             "tests/nuttx-std/link.py", "tests/nuttx-std/prepare-std.py", "tests/rtos_harness/nuttx.py",
             "tests/nuttx-std/check-abi.py", "tools/install-qemu-tools.sh",
             "tools/boot/native_inputs.py", "tools/boot/qualify.py", "tools/boot/target_plan.py", "tools/boot/sign.py",
             "security/boot-dependencies.json", "security/esp32s3-boot-dependencies.json"]
    names.extend(str(path.relative_to(ROOT)) for path in FIXTURE.rglob("*") if path.is_file())
    inputs = {name: process.digest(ROOT / name) for name in sorted(names)}
    native, library, compiler = build_native(work, logs, environment, args.cache)
    images, execution = link_nuttx(work, logs, environment, library, compiler) if args.nuttx else ([], [])
    target.require(all(process.digest(ROOT / name) == digest for name, digest in inputs.items()))
    status, revision = process.run(["git", "rev-parse", "HEAD"], ROOT, environment, logs, "repository-head", 30)
    target.require(status == 0 and re.fullmatch(r"[0-9a-f]{40}\n", revision))
    status, changes = process.run(["git", "status", "--porcelain=v1", "--untracked-files=normal"], ROOT, environment, logs, "repository-state", 30)
    target.require(status == 0)
    report = {"schema": 1, "scope": "actual-xtensa-admission-crypto-compile-and-optional-nuttx-execution", "qualified": True,
        "repository_head": revision.strip(), "repository_has_changes": bool(changes),
        "inputs": inputs, "native": native, "operator_runtime": native_inputs.operator_runtime(),
        "target_header_abi_compiled": True, "nuttx_images_linked": bool(images),
        "nuttx_images": images, "emulated_execution_qualified": bool(execution), "emulated_execution": execution,
        "emulator_is_physical_camera": False, "physical_execution": False,
        "physical_qualified": False, "protected_device_counter": False,
        "activation_qualified": False, "native_advisory_closure": False, "deployment_permitted": False}
    (work / "admission-target-qualification.json").write_bytes(target.canonical(report) + b"\n")
    print("Actual Xtensa SDK crypto and header ABI compiled; execution/device gates remain open")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--nuttx", action="store_true")
    args = parser.parse_args()
    try:
        qualify(args)
    except Exception as error:
        import traceback
        frames = [frame for frame in traceback.extract_tb(error.__traceback__) if Path(frame.filename).parent.resolve() == Path(__file__).parent.resolve()]
        if frames:
            frame = frames[-2] if len(frames) > 1 and frames[-1].name == "require" else frames[-1]
            print("Target admission qualification refused at " + Path(frame.filename).name + ":" + str(frame.lineno), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
