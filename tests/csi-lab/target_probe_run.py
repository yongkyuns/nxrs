#!/usr/bin/env python3
"""Run only the no-radio policy image in pinned QEMU; compare every output word."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
BEGIN = b"CSI_POLICY_BEGIN v=1 radio=0 virtual_time=1 physical_detector_admitted=0"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def complete_lines(data):
    return data[: data.rfind(b"\n") + 1].replace(b"\r\n", b"\n").split(b"\n")[:-1]


def transcript(data):
    lines = complete_lines(data)  # Only UART CRLF is normalized.
    starts = [i for i, line in enumerate(lines) if line == BEGIN]
    if len(starts) != 1:
        raise ValueError("policy transcript begin count")
    start = starts[0]
    ends = [i for i, line in enumerate(lines) if line.startswith(b"CSI_POLICY_END ")]
    if len(ends) != 1 or ends[0] <= start:
        raise ValueError("policy transcript end count/order")
    block = lines[start : ends[0] + 1]
    if any(b"\r" in line for line in block):
        raise ValueError("bare CR in policy data")
    if any(
        not line.startswith((b"CSI_POLICY_CHECK ", b"CSI_POLICY_DISTANCE "))
        for line in block[1:-1]
    ):
        raise ValueError("unexpected data inside policy transcript")
    if any(
        line.startswith(b"CSI_POLICY_") for line in lines[:start] + lines[ends[0] + 1 :]
    ):
        raise ValueError("policy data outside transcript")
    return b"\n".join(block) + b"\n"


def private_write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)


def run(build, qemu, executable, input_path, report_path):
    artifacts = [
        report_path,
        report_path.with_suffix(".console.private.log"),
        report_path.with_suffix(".host.private.log"),
    ]
    if report_path.resolve().is_relative_to(ROOT) or any(
        path.exists() for path in artifacts
    ):
        raise ValueError("use a fresh private report outside checkout")
    receipt = json.loads((build / "reference-build.json").read_text())
    probe = receipt.get("policy_probe", {})
    if (
        probe.get("version") != 1
        or probe.get("radio_entry") is not False
        or probe.get("usb_entry") is not False
    ):
        raise ValueError("not the isolated no-radio probe build")
    if (
        input_path.stat().st_size != probe["input_bytes"]
        or input_path.stat().st_size > 2 * 1024 * 1024
        or probe["input_sha256"] != sha(input_path)
    ):
        raise ValueError("probe input differs from compiled image")
    for name, digest in receipt["source_sha256"].items():
        path = (ROOT / name).resolve()
        if not path.is_relative_to(ROOT) or sha(path) != digest:
            raise ValueError("probe source differs from build receipt")
    image = build / "reference-flash.bin"
    original = sha(image)
    if (
        original != receipt["image_sha256"]
        or sha(build / "build/csi_reference.elf") != receipt["elf_sha256"]
        or sha(build / "sdkconfig") != receipt["config_sha256"]
    ):
        raise ValueError("probe image/configuration differs from receipt")
    version = subprocess.check_output([qemu, "--version"], text=True, timeout=10)
    if "esp_develop_9.0.0_20240606" not in version:
        raise ValueError("requires pinned Espressif QEMU 9.0.0_20240606")
    host = subprocess.run(
        [executable, "run-motion-probe", input_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        timeout=60,
    )
    expected = transcript(host.stdout)
    if len(expected) > 16 * 1024 * 1024:
        raise ValueError("host transcript byte bound")
    command = [
        str(qemu),
        "-machine",
        "esp32s3",
        "-smp",
        "2",
        "-display",
        "none",
        "-serial",
        "stdio",
        "-monitor",
        "none",
        "-nic",
        "none",
        "-no-reboot",
        "-snapshot",
        "-drive",
        f"file={image},if=mtd,format=raw",
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    data = bytearray()
    started = time.monotonic()
    try:
        deadline = started + 180
        while time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], 0.1)
            if ready:
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("QEMU exited before policy completion")
                data.extend(chunk)
                if len(data) > 16 * 1024 * 1024:
                    raise ValueError("policy console byte bound")
                complete = complete_lines(data)
                if any(line.startswith(b"CSI_POLICY_FAIL") for line in complete):
                    raise RuntimeError(
                        "Xtensa policy execution failed; complete reason retained privately"
                    )
                if any(line.startswith(b"CSI_POLICY_END ") for line in complete):
                    break
        else:
            raise TimeoutError("QEMU policy completion deadline")
        actual = transcript(bytes(data))
        if actual != expected:
            raise ValueError("host/Xtensa policy transcript differs")
        report = {
            "image_sha256": original,
            "input_sha256": probe["input_sha256"],
            "host_executable_sha256": sha(executable),
            "qemu_sha256": sha(qemu),
            "qemu_version": version.strip(),
            "normalized_host_output_sha256": hashlib.sha256(expected).hexdigest(),
            "normalized_target_output_sha256": hashlib.sha256(actual).hexdigest(),
            "checkpoint_words_exact": True,
            "checkpoints": sum(
                line.startswith(b"CSI_POLICY_CHECK ") for line in actual.splitlines()
            ),
            "native_comparisons": sum(
                line.startswith(b"CSI_POLICY_DISTANCE ") for line in actual.splitlines()
            ),
            "result": actual.splitlines()[-1].decode("ascii"),
            "emulator_wall_seconds": time.monotonic() - started,
            "radio_or_hardware_accessed": False,
            "physical_cost_qualified": False,
            "scope": "Exact virtual-time Rust feature/policy execution only. Emulator wall time is not target CPU time, radio accuracy, NuttX or simultaneous camera qualification.",
        }
        private_write(report_path, (json.dumps(report, indent=2) + "\n").encode())
        return report
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        private_write(report_path.with_suffix(".console.private.log"), bytes(data))
        private_write(report_path.with_suffix(".host.private.log"), host.stdout)
        if sha(image) != original:
            raise ValueError("QEMU changed immutable probe image")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--build", type=Path, required=True)
    p.add_argument("--qemu", type=Path, required=True)
    p.add_argument("--csi-lab", type=Path, required=True)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    args = p.parse_args()
    report = run(
        args.build.resolve(),
        args.qemu.resolve(),
        args.csi_lab.resolve(),
        args.input.resolve(),
        args.report.resolve(),
    )
    print(
        f"Xtensa policy exact: {report['checkpoints']} checkpoints, {report['native_comparisons']} SDK comparisons; no radio/hardware"
    )


if __name__ == "__main__":
    main()
