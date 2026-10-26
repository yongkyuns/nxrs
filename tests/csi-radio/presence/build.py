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
spec = importlib.util.spec_from_file_location(
    "presence_instrument", HERE / "instrument.py"
)
INSTRUMENT = importlib.util.module_from_spec(spec)
spec.loader.exec_module(INSTRUMENT)

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
    radar = components / "esp-radar/src/esp_radar.c"
    original_radar_sha = hashlib.sha256(radar.read_bytes()).hexdigest()
    radar.write_bytes(INSTRUMENT.radar(radar.read_bytes()))
    provenance = {
        "reference_only": True,
        "source_packages_sha256": PACKAGES,
        "vendor_sources_modified": True,
        "vendor_algorithm_changed": False,
        "source_instrumentation": {
            "original_radar_sha256": original_radar_sha,
            "instrumented_radar_sha256": hashlib.sha256(radar.read_bytes()).hexdigest(),
            "trace_case": "PRESENCE_TRACE",
            "input_ownership": "preprocessing_owner_copies_at_enqueue",
            "disabled_in_other_modes": True,
        },
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
            if not symbols or len(symbols) != 1 or symbols[0]["st_size"] != 40:
                raise ValueError("missing diagnostic ABI witness")
            symbol = symbols[0]
            section = elf.get_section(symbol["st_shndx"])
            offset = symbol["st_value"] - section["sh_addr"]
            layouts.append(struct.unpack("<10I", section.data()[offset : offset + 40]))
    if layouts[0] != layouts[1] or layouts[0] != (
        56,
        4,
        16,
        32,
        35,
        36,
        40,
        44,
        48,
        52,
    ):
        raise ValueError("diagnostic ABI mismatch")
    with (out / "build/csi_reference.elf").open("rb") as stream:
        elf = ELFFile(stream)
        table = elf.get_section_by_name(".symtab")
        trace_layouts = []
        for name in ("presence_trace_c_abi", "presence_trace_rust_abi"):
            symbols = table.get_symbol_by_name(name)
            if not symbols or len(symbols) != 1 or symbols[0]["st_size"] != 56:
                raise ValueError("missing trace ABI witness")
            symbol = symbols[0]
            section = elf.get_section(symbol["st_shndx"])
            offset = symbol["st_value"] - section["sh_addr"]
            trace_layouts.append(
                struct.unpack("<14I", section.data()[offset : offset + 56])
            )
        trace_size = table.get_symbol_by_name("presence_trace_static_bytes")
        if not trace_size or len(trace_size) != 1:
            raise ValueError("missing trace resource witness")
        symbol = trace_size[0]
        section = elf.get_section(symbol["st_shndx"])
        offset = symbol["st_value"] - section["sh_addr"]
        static_bytes = struct.unpack("<I", section.data()[offset : offset + 4])[0]
        pair_size = table.get_symbol_by_name("presence_pair_static_bytes")
        if not pair_size or len(pair_size) != 1 or pair_size[0]["st_size"] != 4:
            raise ValueError("missing calibration pair resource witness")
        symbol = pair_size[0]
        section = elf.get_section(symbol["st_shndx"])
        offset = symbol["st_value"] - section["sh_addr"]
        pair_bytes = struct.unpack("<I", section.data()[offset : offset + 4])[0]
        if not 0 < pair_bytes <= 1024:
            raise ValueError("calibration pair resource budget exceeded")
    with (out / "build/csi_reference.elf").open("rb") as stream:
        elf = ELFFile(stream)
        table = elf.get_section_by_name(".symtab")
        runtime_size = table.get_symbol_by_name("presence_calibration_static_bytes")
        if not runtime_size or len(runtime_size) != 1:
            raise RuntimeError("calibration resource witness missing")
        symbol = runtime_size[0]
        section = elf.get_section(symbol["st_shndx"])
        offset = symbol["st_value"] - section["sh_addr"]
        runtime_bytes = struct.unpack("<I", section.data()[offset : offset + 4])[0]
        symbols = table.get_symbol_by_name("presence_lab_queue_bytes")
        if not symbols or len(symbols) != 1 or symbols[0]["st_size"] != 4:
            raise ValueError("missing continuous recorder resource witness")
        symbol = symbols[0]
        section = elf.get_section(symbol["st_shndx"])
        offset = symbol["st_value"] - section["sh_addr"]
        lab_bytes = struct.unpack("<I", section.data()[offset : offset + 4])[0]
        if not 0 < lab_bytes <= 150000:
            raise ValueError("continuous recorder allocation budget exceeded")
        model_resources = {}
        for resource, limit in (
            ("presence_model_queue_bytes", 16384),
            ("presence_model_control_bytes", 1024),
            ("presence_motion_pipeline_bytes", 4096),
        ):
            symbols = table.get_symbol_by_name(resource)
            if not symbols or len(symbols) != 1 or symbols[0]["st_size"] != 4:
                raise ValueError("missing model ownership resource witness")
            symbol = symbols[0]
            section = elf.get_section(symbol["st_shndx"])
            offset = symbol["st_value"] - section["sh_addr"]
            value = struct.unpack("<I", section.data()[offset : offset + 4])[0]
            if not 0 < value <= limit:
                raise ValueError("model ownership resource budget exceeded")
            model_resources[resource] = value

        if not 0 < runtime_bytes <= 1024:
            raise RuntimeError("calibration resource witness outside fixed budget")
        symbols = table.get_symbol_by_name("presence_failure_static_bytes")
        if not symbols or len(symbols) != 1 or symbols[0]["st_size"] != 4:
            raise ValueError("missing numeric failure resource witness")
        symbol = symbols[0]
        section = elf.get_section(symbol["st_shndx"])
        offset = symbol["st_value"] - section["sh_addr"]
        failure_bytes = struct.unpack("<I", section.data()[offset : offset + 4])[0]
        if not 0 < failure_bytes <= 65536:
            raise ValueError("numeric failure resource budget exceeded")
    if trace_layouts[0] != trace_layouts[1] or trace_layouts[0] != (
        40,
        4,
        0,
        4,
        8,
        12,
        16,
        20,
        24,
        28,
        32,
        36,
        0,
        1,
    ):
        raise ValueError("trace ABI mismatch")
    if not 0 < static_bytes <= 131072:
        raise ValueError("trace static resource budget exceeded")
    import subprocess

    cache = (out / "build/CMakeCache.txt").read_text().splitlines()
    objdump = [
        line.split("=", 1)[1]
        for line in cache
        if line.startswith("CMAKE_OBJDUMP:FILEPATH=")
    ]
    if len(objdump) != 1:
        raise ValueError("missing target disassembler")
    disassembly = subprocess.check_output(
        [
            objdump[0],
            "-d",
            "--disassemble=esp_radar_csi_rx_cb",
            str(out / "build/csi_reference.elf"),
        ],
        text=True,
    )
    linkage = INSTRUMENT.require_compensation_observer(disassembly)
    spec = importlib.util.spec_from_file_location("presence_stack", HERE / "stack.py")
    stack = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stack)
    producer_disassembly = subprocess.check_output(
        [objdump[0], "-d", "-C", str(out / "build/csi_reference.elf")], text=True
    )
    producer_stack = stack.qualify(producer_disassembly)
    arithmetic = IDF.load(
        "presence_pearson_arithmetic", HERE.parent.parent / "csi-lab/arithmetic.py"
    )
    arithmetic_receipt = arithmetic.inspect(out / "build/csi_reference.elf", objdump[0])
    path = out / "reference-build.json"
    receipt = json.loads(path.read_text())
    receipt["pearson_arithmetic"] = arithmetic_receipt
    arithmetic_path = HERE.parent.parent / "csi-lab/arithmetic.py"
    receipt["source_sha256"][str(arithmetic_path.relative_to(IDF.ROOT))] = IDF.sha(
        arithmetic_path
    )
    receipt["motion_archive_sha256"] = IDF.sha(
        out / "components/esp_radar_motion_dec/6.0/esp32s3/libesp_radar_motion_dec.a"
    )
    receipt["presence_reference"] = json.loads(
        (out / "presence-inputs.json").read_text()
    )
    receipt["presence_diagnostic_abi"] = {
        "checks": 10,
        "values": layouts[0],
        "compatible": True,
    }
    receipt["native_failure_capture"] = {
        "opt_in_command": "PRESENCE_FAILURES",
        "static_bytes": failure_bytes,
        "max_matrix_words": 1024,
        "max_cols": 64,
        "first_anomaly_per_scope": True,
        "scopes": ["selected", "other"],
        "other_scope_is_all_nonselected_sdk_calls": True,
        "all_captured_calls_consume_owned_inputs": True,
        "other_inputs_freeze_point": "preprocessing_owner_before_enqueue",
        "native_status_and_output_preserved": True,
        "hardware_qualified": False,
    }
    receipt["calibration_pair_capture"] = {
        "opt_in_command": "PRESENCE_DIAGNOSTIC",
        "static_bytes": pair_bytes,
        "reserved_tags": [14, 15, 16],
        "wire_version": 2,
        "rejection_attribution": True,
        "capture_current_decision": True,
        "rolling_history_samples": 25,
        "private_runtime_data_only": True,
        "records_callback_stack_high_water_at_events": True,
        "instrumentation_overhead_qualified": False,
    }
    receipt["recorder_producer_stack"] = producer_stack
    receipt["continuous_recording"] = {
        "opt_in_command": "PRESENCE_RECORD",
        "wire_version": 1,
        "acknowledgement": "csi_lab_v4 model_owner=control_v1",
        "model_ownership": {
            "owner": "existing application control task",
            "new_workers": 0,
            "source_slots": 64,
            "source_queue_rust_bytes": model_resources["presence_model_queue_bytes"],
            "control_rust_bytes": model_resources["presence_model_control_bytes"],
            "native_mutex_and_allocator_overhead_additional": True,
            "callback_allocations": False,
            "callback_model_mutex": False,
            "control_barrier_wait_ms": 1000,
            "source_drain_before_control_and_labels": True,
            "source_admission_closed_and_quiescent_before_final_drain": True,
            "hardware_qualified": False,
        },
        "packed_wander_record_part": 3,
        "packed_wander_max_columns": 123,
        "packed_wander_inputs_copied_before_native_call": True,
        "legacy_snapshots_enabled": False,
        "preallocated_queue_bytes": lab_bytes,
        "queue_slots": 128,
        "loss_is_fatal_to_dataset": True,
        "native_call_quiescence_before_end": True,
        "callback_allocations": False,
        "hardware_qualified": False,
        "instrumentation_overhead_qualified": False,
    }
    receipt["temporal_motion_trial"] = {
        "opt_in_command": "PRESENCE_MOTION",
        "acknowledgement": "csi_lab_v5 model_owner=control_v1 motion=temporal_motion_v1",
        "case": "temporal-motion-v1",
        "wire_checkpoint_kind": 10,
        "wire_checkpoint_version": 1,
        "wire_checkpoint_words": 32,
        "case_declaration": [4, 1, 16, 0],
        "profile_columns_max": 16,
        "pipeline_heap_rust_bytes": model_resources["presence_motion_pipeline_bytes"],
        "new_workers": 0,
        "callback_model_work": False,
        "owned_vectors": True,
        "shared_device_and_replay_schedule": True,
        "historical_absolute_model_preserved": True,
        "stationary_occupancy_qualified": False,
        "hardware_qualified": False,
        "cpu_cost_qualified": False,
    }
    receipt["calibration_runtime"] = {
        "acknowledgement": "post_training_v2",
        "static_bytes": runtime_bytes,
        "read_contention": "transient_unknown_without_model_failure",
        "lost_source_update": "sticky_rejected",
        "clock_sample": "after_model_access",
        "rejection_reason": "first_reason_sticky",
        "thresholds_and_budgets_changed": False,
        "hardware_qualified": False,
    }
    receipt["snapshot_capture"] = {
        "opt_in_command": "PRESENCE_CAPTURE",
        "frozen_numerical_inputs": True,
        "normal_reference_mode_unchanged": True,
        "max_columns": 64,
        "max_matrix_words": 1024,
        "max_template_vectors": 10,
        "max_phase_samples": 16,
        "max_failure_samples": 1,
        "private_runtime_data_only": True,
        "rf_freshness_qualified": False,
        "instrumentation_overhead_qualified": False,
    }
    receipt["rf_preprocessing_trace"] = {
        "opt_in_command": "PRESENCE_TRACE",
        "abi_checks": 14,
        "abi_values": trace_layouts[0],
        "static_bytes": static_bytes,
        "max_columns": 16,
        "max_rows": 32,
        "max_ring_rows": 64,
        "pending_rf_records": 32,
        "window_slots": 10,
        "export_slots": 1,
        "native_compensation_entry_observed": "esp_csi_gain_ctrl_compensate_rx_gain",
        "physical_qualification": False,
        "linkage": linkage,
    }
    path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        "Full external presence reference built; 10 diagnostic + 14 trace ABI checks and corrected RX linkage match"
    )
