#!/usr/bin/env python3
"""Build the pinned IDF 6.0 reference. No discovery, resets, serial or flashing."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COMMON = load("csi_builder", HERE.parent / "build.py")
BINDINGS = load("idf_bindings", HERE / "libc.py")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def abi_values(path):
    from elftools.elf.elffile import ELFFile

    with path.open("rb") as stream:
        elf = ELFFile(stream)
        table = elf.get_section_by_name(".symtab")
        values = []
        for name in ("reference_c_abi", "reference_rust_abi"):
            symbols = table.get_symbol_by_name(name)
            if not symbols or len(symbols) != 1 or symbols[0]["st_size"] != 204:
                raise ValueError("missing or ambiguous compiled ABI witness")
            symbol = symbols[0]
            section = elf.get_section(symbol["st_shndx"])
            offset = symbol["st_value"] - section["sh_addr"]
            values.append(struct.unpack("<51I", section.data()[offset : offset + 204]))
    if values[0] != values[1]:
        raise ValueError("Rust and IDF native ABI witnesses disagree")
    return values[0]


def check_config(out, project=HERE):
    resolved = {}
    for line in (out / "sdkconfig").read_text().splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            resolved[key] = value
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            resolved[line[2:-11]] = "n"
    for line in (project / "sdkconfig.defaults").read_text().splitlines():
        if line.startswith("CONFIG_"):
            key, expected = line.split("=", 1)
            if resolved.get(key) != expected:
                raise ValueError(f"reference resource configuration mismatch: {key}")


def main(project=HERE, extend_parser=None, prepare=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--idf", type=Path, required=True)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument(
        "--python",
        type=Path,
        required=True,
        help="IDF Python with release dependencies, cmake and ninja on PATH",
    )
    parser.add_argument("--rust-sysroot", type=Path, required=True)
    parser.add_argument("--gain-package", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    if extend_parser:
        extend_parser(parser)
    args = parser.parse_args()
    args.idf, args.tools, args.rust_sysroot = (
        p.resolve() for p in (args.idf, args.tools, args.rust_sysroot)
    )
    out = COMMON.validate_output(args.out)
    pins = json.loads((HERE.parent / "idf60-reference.json").read_text())
    revision = subprocess.check_output(
        ["git", "-C", str(args.idf), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != pins["commit"]:
        raise ValueError("IDF source revision mismatch")
    for name, pin in pins["archives"].items():
        if sha(args.idf / pin["sdk_path"]) != pin["sha256"]:
            raise ValueError(f"IDF archive mismatch: {name}")
    out.mkdir(parents=True, exist_ok=True)
    COMMON.stage_package("esp_csi_gain_ctrl", args.gain_package, out / "vendor")
    if prepare:
        prepare(args, out)
    compiler, libc = BINDINGS.prepare(args.rust_sysroot, out / "rust-sysroot")
    rust_project = out / "rust-project"
    rust_project.mkdir(exist_ok=True)
    manifest = (
        (project / "Cargo.toml")
        .read_text()
        .replace("[lib]", "[lib]\npath = " + json.dumps(str(project / "src/lib.rs")))
    )
    manifest = manifest.replace(
        "../../csi-lab/core", str(HERE.parent.parent / "csi-lab/core")
    )
    manifest += "\n[patch.crates-io]\nlibc = { path = " + json.dumps(str(libc)) + " }\n"
    (rust_project / "Cargo.toml").write_text(manifest)
    for name in ("CMakeLists.txt", "sdkconfig.defaults"):
        shutil.copy2(project / name, out / name)
    shutil.copytree(project / "main", out / "main", dirs_exist_ok=True)
    shutil.copy2(HERE.parent / "ffi/csi.h", out / "capture-abi.h")
    gcc = (
        args.tools
        / "tools/xtensa-esp-elf/esp-15.2.0_20251204/xtensa-esp-elf/bin/xtensa-esp32s3-elf-gcc"
    )
    if not gcc.is_file():
        raise ValueError("install the pinned IDF 6.0 compiler with idf_tools.py")
    environment = dict(
        os.environ,
        IDF_PATH=str(args.idf),
        IDF_TOOLS_PATH=str(args.tools),
        IDF_PYTHON_ENV_PATH=str(args.python.parent.parent),
        ESP_IDF_VERSION="6.0",
        IDF_TARGET="esp32s3",
        IDF_COMPONENT_MANAGER="0",
        RUSTC=str(compiler),
        RUSTDOC=str(args.rust_sysroot / "bin/rustdoc"),
        CARGO_TARGET_DIR=str(out / "rust-target"),
    )
    environment["PATH"] = (
        os.pathsep.join(
            map(str, [args.python.parent, gcc.parent, args.rust_sysroot / "bin"])
        )
        + os.pathsep
        + environment["PATH"]
    )
    for label, command in (
        (
            "rust",
            [
                args.rust_sysroot / "bin/cargo",
                "build",
                "--manifest-path",
                rust_project / "Cargo.toml",
                "--release",
                "--target",
                "xtensa-esp32s3-espidf",
                "-Z",
                "build-std=std,panic_abort",
                "--offline",
            ],
        ),
        (
            "configure",
            [
                args.python,
                args.idf / "tools/idf.py",
                "-C",
                out,
                "-B",
                out / "build",
                "reconfigure",
            ],
        ),
        ("native", ["ninja", "-C", out / "build", "-j", "4"]),
    ):
        with (out / f"{label}.log").open("wb") as log:
            subprocess.run(
                list(map(str, command)),
                env=environment,
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
            )
    check_config(out, project)
    abi = abi_values(out / "build/csi_reference.elf")
    flash = json.loads((out / "build/flasher_args.json").read_text())
    command = [
        args.python,
        "-m",
        "esptool",
        "--chip",
        "esp32s3",
        "merge-bin",
        "--output",
        out / "reference-flash.bin",
        "--flash-mode",
        "dio",
        "--flash-freq",
        "80m",
        "--flash-size",
        "16MB",
        "--fill-flash-size",
        "16MB",
    ]
    for offset, name in flash["flash_files"].items():
        command.extend([offset, out / "build" / name])
    subprocess.run(list(map(str, command)), env=environment, check=True)
    paths = [
        HERE / name
        for name in (
            "Cargo.toml",
            "Cargo.lock",
            "CMakeLists.txt",
            "sdkconfig.defaults",
            "main/CMakeLists.txt",
            "main/sdk.c",
            "src/lib.rs",
            "build.py",
            "libc.py",
            "run.py",
        )
    ]
    paths += [
        HERE.parent / "src" / name
        for name in ("lib.rs", "handoff.rs", "echo.rs", "lifecycle.rs", "traffic.rs")
    ]
    paths += [
        HERE.parent / "ffi/csi.h",
        HERE.parent / "pins.json",
        HERE.parent / "idf60-reference.json",
        HERE.parent / "build.py",
    ]
    if project != HERE:
        paths += [
            project / name
            for name in (
                "Cargo.toml",
                "Cargo.lock",
                "CMakeLists.txt",
                "main/CMakeLists.txt",
                "sdkconfig.defaults",
                "main/sdk.c",
                "src/lib.rs",
                "src/numerical.rs",
                "src/capture.rs",
                "src/trace.rs",
                "src/calibration.rs",
                "src/lab.rs",
                "src/sampling.rs",
                "stack.py",
                "src/diagnostics.rs",
                "src/failures.rs",
                "src/failure_queue.rs",
                "src/source_gate.rs",
                "replay.py",
                "failures.py",
                "calibration_pairs.py",
                "main/trace.h",
                "instrument.py",
                "trace.py",
                "build.py",
                "run.py",
                "snapshots.py",
            )
        ]
    if project != HERE:
        core = HERE.parent.parent / "csi-lab/core"
        paths += [core / "Cargo.toml"] + sorted((core / "src").glob("*.rs"))
    receipt = dict(
        idf_commit=revision,
        target="esp32s3",
        image_sha256=sha(out / "reference-flash.bin"),
        elf_sha256=sha(out / "build/csi_reference.elf"),
        config_sha256=sha(out / "sdkconfig"),
        source_sha256={str(p.relative_to(ROOT)): sha(p) for p in paths},
        radio_archives_sha256={
            p["sdk_path"]: sha(args.idf / p["sdk_path"])
            for p in pins["archives"].values()
        },
        gain_archive_sha256=sha(out / "vendor/6.0/esp32s3/libesp_csi_gain_ctrl.a"),
        rust_library_sha256=sha(
            out / "rust-target/xtensa-esp32s3-espidf/release/libcsi_idf60_reference.a"
        ),
        private_libc_newlib_sha256=sha(libc / "src/unix/newlib/mod.rs"),
        private_libc_espidf_sha256=sha(libc / "src/unix/newlib/espidf/mod.rs"),
        gcc_sha256=sha(gcc),
        rustc_sha256=sha(args.rust_sysroot / "bin/rustc"),
        abi=dict(checks=51, values=abi, compatible=True),
        hardware_qualified=False,
        gain_forcing=False,
    )
    (out / "reference-build.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        "IDF 6.0 reference built; 51 native ABI checks match; hardware remains unqualified"
    )
    return out


if __name__ == "__main__":
    main()
