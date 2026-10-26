#!/usr/bin/env python3
"""Build a UART/no-radio Xtensa policy witness; never discover or touch USB.

The input blob is private and embedded in this emulator-only image. The normal
standalone camera/CSI image and its configuration remain separate artifacts.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import struct

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PRESENCE = ROOT / "tests/csi-radio/presence"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stage(destination, data):
    if destination.exists():
        raise ValueError("probe source destination already exists")
    if data.stat().st_size > 2 * 1024 * 1024 or data.stat().st_mode & 0o077:
        raise ValueError("probe input must be owner-only and at most 2 MiB")
    if data.resolve().is_relative_to(ROOT):
        raise ValueError("keep raw probe inputs outside checkout")
    if data.read_bytes()[:8] != b"CSIMP001":
        raise ValueError("probe input version")
    shutil.copytree(
        PRESENCE, destination, ignore=shutil.ignore_patterns("target", "__pycache__")
    )
    source = destination / "src/lib.rs"
    original = source.read_text()
    old_path = '#[path = "../../idf60/src/lib.rs"]'
    if original.count(old_path) != 1:
        raise ValueError("ambiguous probe SDK source path")
    source_path = json.dumps(str(ROOT / "tests/csi-radio/idf60/src/lib.rs"))
    original = original.replace(old_path, f"#[path = {source_path}]")
    marker = '\n#[no_mangle]\npub extern "C" fn presence_main() {'
    if original.count(marker) != 1:
        raise ValueError("ambiguous probe entry")
    prefix = original[: original.index(marker)]
    probe_path = json.dumps(str(HERE / "target_probe.rs"))
    input_path = json.dumps(str(data.resolve()))
    source.write_text(
        prefix
        + f"""\n#[path = {probe_path}]
mod policy_probe;
#[no_mangle]
pub extern "C" fn presence_main() {{
    // Pure numerical/policy replay: deliberately no reference USB or netif calls.
    std::panic::set_hook(Box::new(|_| eprintln!("CSI_POLICY_FAIL reason=panic")));
    let input=include_bytes!({input_path});
    let mut output=std::io::stdout().lock();
    if let Err(reason)=policy_probe::run(input,&mut output) {{
        let _=writeln!(&mut output,"CSI_POLICY_FAIL reason={{reason}}");
    }}
    loop {{std::thread::sleep(std::time::Duration::from_secs(1));}}
}}
"""
    )
    config = destination / "sdkconfig.defaults"
    lines = [
        line
        for line in config.read_text().splitlines()
        if not line.startswith("CONFIG_SPIRAM")
    ]
    lines = [
        line.replace(
            "CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y",
            "CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=n\nCONFIG_ESP_CONSOLE_UART_DEFAULT=y",
        )
        for line in lines
    ]
    config.write_text("\n".join(lines) + "\nCONFIG_SPIRAM=n\n")


def native_abi(elf_path):
    from elftools.elf.elffile import ELFFile

    with elf_path.open("rb") as stream:
        elf = ELFFile(stream)
        table = elf.get_section_by_name(".symtab")
        for prefix, count in (("presence", 10), ("presence_trace", 14)):
            values = []
            for language in ("c", "rust"):
                entries = table.get_symbol_by_name(f"{prefix}_{language}_abi")
                if (
                    not entries
                    or len(entries) != 1
                    or entries[0]["st_size"] != count * 4
                ):
                    raise ValueError("probe native ABI witness missing")
                symbol = entries[0]
                section = elf.get_section(symbol["st_shndx"])
                at = symbol["st_value"] - section["sh_addr"]
                values.append(
                    struct.unpack(f"<{count}I", section.data()[at : at + count * 4])
                )
            if values[0] != values[1]:
                raise ValueError("probe native ABI differs")


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--probe-input", type=Path, required=True)
    early, _ = parser.parse_known_args()
    reference = load("csi_policy_reference_build", PRESENCE / "build.py")
    out = reference.IDF.COMMON.validate_output(early.out)
    if out.exists():
        raise ValueError("probe output must be fresh")
    project = out.with_name(out.name + "-source")
    stage(project, early.probe_input)
    input_digest = hashlib.sha256(early.probe_input.read_bytes()).hexdigest()
    locked = {
        p: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in HERE.joinpath("core/src").glob("*.rs")
    }
    locked[HERE / "target_probe.rs"] = hashlib.sha256(
        (HERE / "target_probe.rs").read_bytes()
    ).hexdigest()

    def extend(options):
        reference.extend(options)
        options.add_argument("--probe-input", type=Path, required=True)

    built = reference.IDF.main(project, extend, reference.prepare)
    if any(
        hashlib.sha256(p.read_bytes()).hexdigest() != digest
        for p, digest in locked.items()
    ):
        raise ValueError("probe sources changed during build")
    if hashlib.sha256(early.probe_input.read_bytes()).hexdigest() != input_digest:
        raise ValueError("probe input changed during build")
    native_abi(built / "build/csi_reference.elf")
    arithmetic = load("csi_policy_arithmetic", HERE / "arithmetic.py")
    cache = (built / "build/CMakeCache.txt").read_text().splitlines()
    objdump = [
        line.split("=", 1)[1]
        for line in cache
        if line.startswith("CMAKE_OBJDUMP:FILEPATH=")
    ]
    if len(objdump) != 1:
        raise ValueError("missing target disassembler")
    arithmetic_receipt = arithmetic.inspect(
        built / "build/csi_reference.elf", objdump[0]
    )
    path = built / "reference-build.json"
    receipt = json.loads(path.read_text())
    for p, digest in locked.items():
        receipt["source_sha256"][str(p.relative_to(ROOT))] = digest
    receipt["source_sha256"][str(Path(__file__).relative_to(ROOT))] = hashlib.sha256(
        Path(__file__).read_bytes()
    ).hexdigest()
    receipt["source_sha256"][str((HERE / "arithmetic.py").relative_to(ROOT))] = (
        hashlib.sha256((HERE / "arithmetic.py").read_bytes()).hexdigest()
    )
    receipt["pearson_arithmetic"] = arithmetic_receipt
    receipt["policy_probe"] = {
        "version": 1,
        "input_sha256": input_digest,
        "input_bytes": early.probe_input.stat().st_size,
        "abi_checks": 75,
        "console": "UART",
        "radio_entry": False,
        "usb_entry": False,
        "virtual_time": True,
        "hardware_qualified": False,
        "physical_cost_qualified": False,
        "scope": "Same Rust core, emulator-only entry/configuration; private inputs embedded. Not a radio, NuttX, camera or real-time performance witness.",
    }
    path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        "Pure Xtensa policy probe built; 75 native ABI checks match; no hardware accessed"
    )


if __name__ == "__main__":
    main()
