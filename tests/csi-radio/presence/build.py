#!/usr/bin/env python3
"""Build the complete external Espressif presence reference; never touches USB."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import zipfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "idf_builder", HERE.parent / "idf60/build.py"
)
IDF = importlib.util.module_from_spec(spec)
spec.loader.exec_module(IDF)

PACKAGES = {
    "esp-radar": "4df10bb2aaf3703ea1f311e19dcf886d6e5b4f60aa0420a4150f5043db234590",
    "esp_wifi_sensing": "4ed1ce323b63fe6fa7a98fcc5974dc65946f2d891e95cc5483697179e1f1d814",
}


def extract_source(package, name, destination):
    if hashlib.sha256(package.read_bytes()).hexdigest() != PACKAGES[name]:
        raise ValueError("reference source package mismatch")
    with zipfile.ZipFile(package) as archive:
        for item in archive.infolist():
            path = Path(item.filename)
            if path.parts[0] != name or ".." in path.parts or path.is_absolute():
                raise ValueError("unsafe package path")
            relative = Path(*path.parts[1:])
            if not item.is_dir() and (
                relative.parts[0] in ("src", "include")
                or str(relative) in ("LICENSE", "Kconfig")
            ):
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(item))


def extend(parser):
    parser.add_argument("--radar-package", type=Path, required=True)
    parser.add_argument("--sensing-package", type=Path, required=True)
    parser.add_argument("--motion-package", type=Path, required=True)


def prepare(args, out):
    components = out / "components"
    for name, package, dependencies in (
        (
            "esp-radar",
            args.radar_package,
            "esp_wifi esp_timer esp_csi_gain_ctrl esp_radar_motion_dec",
        ),
        (
            "esp_wifi_sensing",
            args.sensing_package,
            "esp_wifi esp_timer esp-radar nvs_flash",
        ),
    ):
        destination = components / name
        extract_source(package, name, destination)
        (destination / "CMakeLists.txt").write_text(
            'idf_component_register(SRC_DIRS "src" INCLUDE_DIRS "include" REQUIRES '
            + dependencies
            + ')\ntarget_compile_options(${COMPONENT_LIB} PRIVATE "-Wno-format")\n'
        )
    for name, package in (
        ("esp_csi_gain_ctrl", args.gain_package),
        ("esp_radar_motion_dec", args.motion_package),
    ):
        destination = components / name
        IDF.COMMON.stage_package(name, package, destination)
        (destination / "CMakeLists.txt").write_text(
            'idf_component_register(INCLUDE_DIRS "include" REQUIRES log esp_phy)\n'
            f'add_prebuilt_library({name}_binary "${{CMAKE_CURRENT_LIST_DIR}}/6.0/esp32s3/lib{name}.a" REQUIRES log esp_phy)\n'
            f"target_link_libraries(${{COMPONENT_LIB}} INTERFACE {name}_binary)\n"
        )
    (out / "main").mkdir(exist_ok=True)
    shutil.copy2(IDF.HERE / "main/sdk.c", out / "main/capture-sdk.c")
    provenance = {
        "reference_only": True,
        "source_packages_sha256": PACKAGES,
        "vendor_sources_modified": False,
        "build_wiring": "local CMake registration, no component manager helpers",
        "source_files_sha256": {
            str(p.relative_to(components)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in components.rglob("*")
            if p.is_file()
        },
        "production_nuttx_integration": False,
        "gain_forcing": False,
        "receiver_promiscuous_mode": True,
        "router_ping_hz": 100,
        "channel_count": 1,
    }
    (out / "presence-inputs.json").write_text(json.dumps(provenance, indent=2) + "\n")


if __name__ == "__main__":
    out = IDF.main(HERE, extend, prepare)
    # The diagnostic bridge has its own native layout witness in addition to
    # the 51 shared SDK/libc checks. Refuse a mismatched image before flashing.
    from elftools.elf.elffile import ELFFile
    import struct

    with (out / "build/csi_reference.elf").open("rb") as stream:
        elf = ELFFile(stream)
        table = elf.get_section_by_name(".symtab")
        layouts = []
        for name in ("presence_c_abi", "presence_rust_abi"):
            symbols = table.get_symbol_by_name(name)
            if not symbols or len(symbols) != 1 or symbols[0]["st_size"] != 20:
                raise ValueError("missing diagnostic ABI witness")
            symbol = symbols[0]
            section = elf.get_section(symbol["st_shndx"])
            offset = symbol["st_value"] - section["sh_addr"]
            layouts.append(struct.unpack("<5I", section.data()[offset : offset + 20]))
    if layouts[0] != layouts[1] or layouts[0] != (36, 4, 16, 32, 35):
        raise ValueError("diagnostic ABI mismatch")
    path = out / "reference-build.json"
    receipt = json.loads(path.read_text())
    receipt["presence_reference"] = json.loads(
        (out / "presence-inputs.json").read_text()
    )
    receipt["presence_diagnostic_abi"] = {
        "checks": 5,
        "values": layouts[0],
        "compatible": True,
    }
    path.write_text(json.dumps(receipt, indent=2) + "\n")
    print("Full external presence reference built; 5 diagnostic ABI checks match")
