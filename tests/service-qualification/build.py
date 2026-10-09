#!/usr/bin/env python3
"""Local paired build. Reuse a pinned private SDK and frozen compiler package.

No dependency changes, SDK installation, CI activation or flashing. The Rust
compile can run in Linux; the final links can use the existing macOS SDK tree.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def run(command, log, **kwargs):
    with log.open("w") as output:
        subprocess.run(list(map(str, command)), stdout=output,
                       stderr=subprocess.STDOUT, check=True, **kwargs)


TRACE_WRAPS = ("nxrs_sq_run", "nxrs_sq_wait", "nxrs_sq_led_apply", "nxrs_sq_record")
FAULT_WRAPS = ("nxrs_sq_run", "mq_open", "mq_unlink", "mq_send", "mq_close",
               "nxrs_sq_record", "nxrs_cq_thread_start", "nxrs_cq_thread_join")
HOT_IRAM_SELECTORS = (
    "*libapps.a:*runtime.c.*", "*libapps.a:*hal_nuttx.c.*",
    "*libapps.a:*native_thread.c.*", "*libapps.a:*worker.c.*",
    "*libfs.a:*fs_poll.o", "*libsched.a:*mq_*.o",
)


def diagnostic_sections(text, *, padding=False, hot_iram=False):
    """Derive a diagnostic script; never edit the dependency's linker script."""
    if padding:
        marker = "    _instruction_reserved_start = ABSOLUTE(.);\n"
        if text.count(marker) != 1:
            raise ValueError("expected one flash placement marker")
        text = text.replace(marker, marker + "    KEEP(*(.text.nxrs_sq_layout_padding))\n")
    if hot_iram:
        marker = "    /* Code marked as running out of IRAM */\n"
        if text.count(marker) != 1:
            raise ValueError("expected one IRAM placement marker")
        selectors = "\n" + "\n".join(
            f"    {name}(.literal .text .literal.* .text.*)" for name in HOT_IRAM_SELECTORS)
        selectors += "\n    *(.literal.nxrs_sq_worker .text.nxrs_sq_worker)\n"
        text = text.replace(marker, marker + selectors)
    return text


def diagnostic_scripts(tree, out, env, variables, *, padding=False, hot_iram=False):
    command = ["make", "-s", "-C", str(tree / "nuttx/arch/xtensa/src"),
               "-f", "Makefile", "-f", "-", "nxrs_print_scripts",
               "TOPDIR=" + str(tree / "nuttx"), *variables]
    extra = "nxrs_print_scripts:\n\t@printf 'SQ_ARCHSCRIPTS=%s\\n' '$(ARCHSCRIPT)'\n"
    output = subprocess.run(command, input=extra, env=env, text=True,
                            capture_output=True, check=True).stdout
    rows = [line.removeprefix("SQ_ARCHSCRIPTS=") for line in output.splitlines()
            if line.startswith("SQ_ARCHSCRIPTS=")]
    if len(rows) != 1:
        raise ValueError("could not resolve board linker scripts")
    scripts = rows[0].split()
    candidates = [Path(path) for path in scripts if Path(path).name == "esp32s3_sections.ld"]
    if len(candidates) != 1:
        raise ValueError("diagnostic requires the modern ESP32-S3 sections script")
    original = candidates[0]
    generated = out / "diagnostic-sections.ld"
    generated.write_text(diagnostic_sections(original.read_text(), padding=padding, hot_iram=hot_iram))
    scripts[scripts.index(str(original))] = str(generated)
    variables.append("ARCHSCRIPT=" + " ".join(scripts))
    return dict(base_script_sha256=digest(original), generated_script_sha256=digest(generated),
                hot_iram_selectors=list(HOT_IRAM_SELECTORS) if hot_iram else [])


def source_inputs(language, trace=False, dual_worker=False, entry_switch=False,
                  layout_pad_bytes=None, perfmon=False, faults=False):
    common = [HERE / "runtime.c", HERE / "qualification.h", HERE / "pulse_snapshot.h",
              ROOT / "tests/service-footprint/native_thread.c"]
    common += [ROOT / "tests/event-services-comparison" / name
               for name in ("hal_nuttx.c", "hal.h", "platform.h", "contract.h", "clock.h")]
    if trace:
        common.append(HERE / "trace.c")
    if dual_worker:
        common.extend([HERE / "worker_switch.c", HERE / "worker.c"])
    if entry_switch:
        common.append(HERE / "entry_switch.c")
    if layout_pad_bytes is not None:
        common.insert(0, HERE / "layout_padding.c")
    if perfmon:
        common.append(HERE / "perfmon.c")
    if faults:
        common.extend(HERE / name for name in
                      ("lifecycle_faults.c", "lifecycle_faults.h", "lifecycle_device.c"))
    return common + ([HERE / "worker.c"] if language == "c" else
                     [HERE / name for name in ("src/main.rs", "Cargo.toml", "Cargo.lock", "build.rs")])


def stage_native_app(tree, language, trace=False, dual_worker=False, entry_switch=False,
                     layout_pad_bytes=None, perfmon=False, faults=False):
    rust = language == "rust"
    app = tree / "apps/examples" / ("nxrs_std_app" if rust else "nxrs_bench")
    app.mkdir(parents=True, exist_ok=True)
    sources = source_inputs(language, trace, dual_worker, entry_switch, layout_pad_bytes, perfmon, faults)
    native = []
    for source in sources:
        if source.suffix not in (".c", ".h"): continue
        shutil.copy2(source, app / source.name)
        if source.suffix == ".c" and source.name != "worker.c": native.append(source.name)
    if layout_pad_bytes is not None:
        (app / "sq_layout_padding.h").write_text(
            "#define SQ_LAYOUT_PADDING_BYTES " + str(layout_pad_bytes) + "\n")
    if rust:
        makefile = (ROOT / "platform/nuttx/std-app/Makefile").read_text()
        if entry_switch:
            makefile = makefile.replace("main=$(NXRS_APP_COMMAND)_main", "main=sq_std_entry")
    else:
        (app / "Kconfig").write_text('config EXAMPLES_NXRS_BENCH\n\ttristate "C service qualification"\n\tdefault n\n')
        (app / "Make.defs").write_text('ifneq ($(CONFIG_EXAMPLES_NXRS_BENCH),)\nCONFIGURED_APPS += $(APPDIR)/examples/nxrs_bench\nendif\n')
        makefile = ('include $(APPDIR)/Make.defs\nPROGNAME = sq_c\nPRIORITY = 100\nSTACKSIZE = 8192\n'
                    'MODULE = $(CONFIG_EXAMPLES_NXRS_BENCH)\nMAINSRC = worker.c\n'
                    'CSRCS += $(NXRS_TARGET_C_SOURCE)\nCFLAGS += $(NXRS_TARGET_C_FLAGS)\n'
                    'include $(APPDIR)/Application.mk\n')
    (app / "Makefile").write_text(makefile)
    return sources, native


def make_variables(prefix, language, native, bundle=None, trace=False, dual_worker=False,
                   layout_pad_bytes=None, perfmon=False, faults=False):
    variables = ["CROSSDEV=" + Path(prefix).name, "ESPTOOL_BINDIR=.",
                 "NXRS_APP_COMMAND=sq_" + language, "NXRS_APP_PRIORITY=100", "NXRS_APP_STACKSIZE=8192",
                 "NXRS_TARGET_C_SOURCE=" + " ".join(native), "NXRS_TARGET_C_FLAGS=-std=c11 -O2"]
    if language == "rust": variables += ["NXRS_STD_ELF=" + str(bundle.resolve() / "rust-input.elf")]
    link_commands = []
    if trace:
        wraps = TRACE_WRAPS + (("nxrs_sq_worker",) if dual_worker else ())
        link_commands.extend("--wrap=" + name for name in wraps)
    if layout_pad_bytes is not None:
        link_commands.append("--undefined=nxrs_sq_layout_padding")
    if perfmon:
        link_commands.extend(("--wrap=nxrs_sq_ready", "--wrap=nxrs_cq_thread_join"))
    if faults:
        link_commands.extend("--wrap=" + name for name in FAULT_WRAPS)
    if link_commands:
        variables.append("EXTRALINKCMDS=" + " ".join(link_commands))
    return variables


def rust_input(args):
    # Reuse the arithmetic package identity validation, not its test protocol.
    sys.path.insert(0, str(ROOT / "tests/arithmetic-parity"))
    arithmetic = load("sq_compiler", ROOT / "tests/arithmetic-parity/build.py")
    sysroot, out = args.sysroot.resolve(), args.out.resolve()
    library = sysroot / "lib/rustlib/src/rust/library"
    inventory = {str(p.relative_to(library)): digest(p) for p in sorted(library.rglob("*")) if p.is_file()}
    ledger = json.loads(args.ledger.read_text())
    package = arithmetic.validate_compiler(sysroot, inventory, ledger, args.compiler_provenance)
    frozen = json.loads((ROOT / "tests/arithmetic-parity/results/compiler-mul-range-2026-10-07.json").read_text())
    patch_identity = lambda rows: [(row["name"], row["sha256"]) for row in rows]
    if (ledger["upstream_revision"] != frozen["llvm_revision"] or
            ledger["rust_revision"] != frozen["rust_revision"] or
            patch_identity(ledger["patches"]) != patch_identity(frozen["patches"])):
        raise ValueError("use the frozen 29-patch mul-range qualification package")
    for patch in ledger["patches"]:
        if Path(patch["name"]).name != patch["name"]:
            raise ValueError("unsafe patch name")
        folder = "proposals" if patch.get("proposal") else "patches"
        if digest(ROOT / "upstream/rust-llvm" / folder / patch["name"]) != patch["sha256"]:
            raise ValueError("public patch bytes changed")
    out.mkdir(parents=True, exist_ok=False)
    wrapper = ROOT / "tests/nuttx-std/link.py"
    env = dict(os.environ, RUSTC_BOOTSTRAP="1", RUSTUP_TOOLCHAIN="1.90.0",
               RUSTC=str(sysroot / "bin/rustc"), RUSTDOC=str(sysroot / "bin/rustdoc"),
               NUTTX_STD_SYSROOT=str(sysroot), NUTTX_STD_GNU_LINKER=str(args.ld.resolve()),
               NUTTX_STD_LINK_LOG=str(out / "rust-link.json"),
               CARGO_TARGET_DIR=str(args.cargo_target.resolve()),
               RUSTFLAGS="-C panic=abort -C linker=" + str(wrapper))
    env["PATH"] = str(args.ld.resolve().parent) + os.pathsep + env.get("PATH", "")
    command = [args.cargo.resolve(), "--config", "build.jobs=1", "rustc", "--release", "--offline", "--locked",
               "--target", args.target.resolve(), "-Zbuild-std=std,panic_abort",
               "-Zbuild-std-features=" + ",".join(arithmetic.STD_FEATURES),
               "--message-format=json-render-diagnostics",
               # App-only root: C reaches this callback. Do not invalidate the
               # entire std build for an application's partial-link root.
               "--", "-C", "link-arg=-unxrs_sq_worker"]
    with (out / "cargo-messages.jsonl").open("w") as stream, (out / "cargo.log").open("w") as log:
        subprocess.run(list(map(str, command)), cwd=HERE, env=env, stdout=stream, stderr=log, check=True)
    messages = [json.loads(line) for line in (out / "cargo-messages.jsonl").read_text().splitlines()]
    std = [row for row in messages if row.get("reason") == "compiler-artifact" and row["target"]["name"] == "std"]
    app = [row for row in messages if row.get("reason") == "compiler-artifact" and row["target"]["name"] == "nxrs-service-qualification"]
    if len(std) != 1 or Path(std[0]["target"]["src_path"]).resolve() != (library / "std/src/lib.rs").resolve():
        raise ValueError("Cargo selected a different std snapshot")
    if len(app) != 1 or app[0]["profile"]["opt_level"] != "2":
        raise ValueError("missing native input or changed application policy")
    if not (out / "rust-input.elf").is_file():
        executable = Path(app[0]["executable"])
        header = subprocess.check_output([str(args.ld.resolve())[:-2] + "readelf", "-h", executable], text=True)
        if "REL (Relocatable file)" not in header or "Xtensa" not in header:
            raise ValueError("Cargo cache hit is not a native relocatable Xtensa input")
        shutil.copy2(executable, out / "rust-input.elf")
    after = {str(p.relative_to(library)): digest(p) for p in sorted(library.rglob("*")) if p.is_file()}
    if after != inventory:
        raise ValueError("std changed during compilation")
    write_json(out / "compiler-input.json", dict(
        schema=1, input_sha256=digest(out / "rust-input.elf"),
        source_sha256={str(p.relative_to(ROOT)): digest(p) for p in source_inputs("rust")},
        target_sha256=digest(args.target), compiler_package_sha256=digest(args.compiler_provenance),
        link_wrapper_sha256=digest(wrapper), rustflags=env["RUSTFLAGS"], link_roots=["main", "nxrs_sq_worker"],
        partial_linker_sha256=digest(args.ld), builder_sha256=digest(Path(__file__)),
        patch_ledger=ledger, std_features=arithmetic.STD_FEATURES,
        std_inventory_sha256=hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest(),
        compiler_driver_sha256=package["rustc_driver_library_sha256"], application_opt_level="2"))
    print("SERVICE_RUST_INPUT_PASS", out)


def build_env(prefix):
    if not Path(prefix + "gcc").is_file():
        raise ValueError("explicit target compiler prefix does not exist")
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(Path(prefix).parent), str(ROOT / "target/zephyr-python/bin"),
                                 "/usr/local/opt/gnu-sed/libexec/gnubin", env.get("PATH", "")])
    return env


def prepare(args):
    tree, out = args.tree.resolve(), args.out.resolve()
    helpers = load("sq_relink", ROOT / "tests/service-footprint/relink_rust.py")
    with helpers.lock_build_tree(tree):
        out.mkdir(parents=True, exist_ok=False)
        shutil.copy2(tree / "nuttx/.config", out / "original.config")
        env = build_env(args.prefix)
        _, native = stage_native_app(tree, "c")
        run(["bash", tree / "apps/tools/mkkconfig.sh", "-m", "Examples"],
            out / "examples.log", cwd=tree / "apps/examples", env=env)
        for option in ("CONFIG_DEV_GPIO", "CONFIG_ESP32S3_GPIO_IRQ"):
            subprocess.run(["kconfig-tweak", "--enable", option], cwd=tree / "nuttx", env=env, check=True)
        subprocess.run(["kconfig-tweak", "--enable", "CONFIG_EXAMPLES_NXRS_BENCH"], cwd=tree / "nuttx", env=env, check=True)
        subprocess.run(["kconfig-tweak", "--disable", "CONFIG_EXAMPLES_NXRS_STD_APP"], cwd=tree / "nuttx", env=env, check=True)
        kconfig = ["CROSSDEV=" + Path(args.prefix).name,
                   "KCONFIG_OLDDEFCONFIG=" + str(ROOT / "target/zephyr-python/bin/python") + " -m olddefconfig"]
        run(["make", "olddefconfig", *kconfig], out / "config.log", cwd=tree / "nuttx", env=env)
        text = (tree / "nuttx/.config").read_text()
        if any(option + "=y\n" not in text for option in ("CONFIG_DEV_GPIO", "CONFIG_ESP32S3_GPIO_IRQ")):
            raise ValueError("native GPIO/IRQ configuration unavailable")
        # A real kernel rebuild is required when enabling a driver. All later
        # C/Rust links require this same resolved configuration and archives.
        variables = make_variables(args.prefix, "c", native)
        run(["make", "clean", *variables], out / "clean.log", cwd=tree / "nuttx", env=env)
        with (out / "build.log").open("w") as log:
            helpers.refresh_registration(tree, variables, env, log)
            subprocess.run(["make", "-j2", "--old-file=arch/xtensa/src/.context", *variables],
                           cwd=tree / "nuttx", env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        shutil.copy2(tree / "nuttx/.config", out / "resolved.config")
    print("SERVICE_KERNEL_PREPARED", out)


def final_link(args):
    tree, out, baseline = args.tree.resolve(), args.out.resolve(), args.baseline.resolve()
    helpers = load("sq_relink", ROOT / "tests/service-footprint/relink_rust.py")
    config_helper = load("sq_config", ROOT / "tests/rtos-bench/build.py")
    kernel = load("sq_kernel", ROOT / "tests/arithmetic-parity/kernel_evidence.py")
    sections = load("sq_sections", ROOT / "tests/zephyr-comparison/build.py")
    env = build_env(args.prefix)
    prefix = str(args.prefix)
    with helpers.lock_build_tree(tree):
        identity = config_helper.config_identity(baseline)
        if config_helper.config_identity(tree / "nuttx/.config") != identity:
            raise ValueError("prepared kernel configuration differs from baseline")
        out.mkdir(parents=True, exist_ok=False)
        rust = args.language == "rust"
        command_name = "sq_rust" if rust else "sq_c"
        libraries = kernel.kernel_inventory(tree, prefix)
        uname_before = kernel.freeze_utsname(tree, prefix, out, "uname-before.o")
        for option, enable in (("CONFIG_EXAMPLES_NXRS_STD_APP", rust), ("CONFIG_EXAMPLES_NXRS_BENCH", not rust)):
            subprocess.run(["kconfig-tweak", "--enable" if enable else "--disable", option],
                           cwd=tree / "nuttx", env=env, check=True)
        if rust:
            proof = json.loads((args.bundle / "compiler-input.json").read_text())
            if digest(args.bundle / "rust-input.elf") != proof["input_sha256"]:
                raise ValueError("Rust input changed")
            if digest(tree / "xtensa-esp32s3-nuttx.json") != proof["target_sha256"]:
                raise ValueError("Rust/final target specification differs")
            for name, expected in proof["source_sha256"].items():
                if digest(ROOT / name) != expected: raise ValueError("Rust build source changed")
        else:
            proof = None
        sources, native = stage_native_app(tree, args.language, args.trace, args.dual_worker,
                                           args.entry_switch, args.layout_pad_bytes, args.perfmon, args.faults)
        variables = make_variables(prefix, args.language, native, args.bundle, args.trace,
                                   args.dual_worker, args.layout_pad_bytes, args.perfmon, args.faults)
        layout = None
        if args.layout_pad_bytes is not None or args.hot_iram:
            layout = diagnostic_scripts(tree, out, env, variables,
                                        padding=args.layout_pad_bytes is not None, hot_iram=args.hot_iram)
        with (out / "build.log").open("w") as log:
            helpers.refresh_registration(tree, variables, env, log)
            # App selection is a Makefile concern here. Preserve the compiled
            # kernel header to avoid changing DWARF input hashes/archives for
            # app-only macros; all other config differences are rejected above.
            subprocess.run(["make", "-j2", "--old-file=arch/xtensa/src/.context",
                            "--old-file=" + str(tree / "nuttx/include/nuttx/config.h"),
                            "--old-file=" + str(tree / "nuttx/.config"), *variables],
                           cwd=tree / "nuttx", env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        if config_helper.config_identity(tree / "nuttx/.config") != identity or kernel.kernel_inventory(tree, prefix) != libraries:
            raise ValueError("kernel config/archive inputs changed during application link")
        kernel.timestamp_only(uname_before, kernel.freeze_utsname(tree, prefix, out, "uname-after.o"), prefix)
        for original, saved in (("nuttx", "app.elf"), ("nuttx.bin", "image.bin"), (".config", "resolved.config"), ("nuttx.map", "final.map")):
            shutil.copy2(tree / "nuttx" / original, out / saved)
        symbol_text = subprocess.check_output([prefix + "nm", "-C", out / "app.elf"], text=True)
        if command_name + "_main" not in symbol_text or " nxrs_sq_worker" not in symbol_text:
            raise ValueError("qualification app missing from final firmware")
        if not rust and any(token in symbol_text for token in ("std::", "core::", "alloc::", "__rust_")):
            raise ValueError("C firmware contains Rust code")
        (out / "symbols.txt").write_text(symbol_text)
        section_text = subprocess.check_output([prefix + "readelf", "-W", "-S", out / "app.elf"], text=True)
        (out / "sections.txt").write_text(section_text)
        accounting = sections.section_accounting(sections.parse_sections(section_text))
        write_json(out / "build-provenance.json", dict(schema=1, language=args.language, command=command_name,
            config_identity=identity, kernel_archives=libraries, compiler_input=proof,
            kernel_header_sha256=digest(tree / "nuttx/include/nuttx/config.h"),
            dependency_ledgers={p.name: json.loads(p.read_text()) for p in
                                (tree / "nuttx-patches.json", tree / "nuttx-apps-patches.json") if p.is_file()},
            c_compiler_sha256=digest(prefix + "gcc"), c_flags="-std=c11 -O2", thread_stack=4096,
            diagnostic_trace=args.trace,
            diagnostic_worker_switch=args.dual_worker,
            diagnostic_entry_switch=args.entry_switch,
            diagnostic_perfmon=args.perfmon,
            diagnostic_faults=args.faults,
            diagnostic_hot_iram=args.hot_iram,
            diagnostic_linker_script=layout,
            diagnostic_layout_padding_bytes=args.layout_pad_bytes,
            diagnostic_layout_padding_header_sha256=(
                digest(tree / "apps/examples" / ("nxrs_std_app" if rust else "nxrs_bench") /
                       "sq_layout_padding.h") if args.layout_pad_bytes is not None else None),
            source_sha256={str(p.relative_to(ROOT)): digest(p) for p in sources},
            artifacts={name: digest(out / name) for name in ("app.elf", "image.bin", "resolved.config")},
            binary_bytes=(out / "image.bin").stat().st_size, accounting=accounting))
    print("SERVICE_FINAL_LINK_PASS", args.language, out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    phases = parser.add_subparsers(dest="phase", required=True)
    rust = phases.add_parser("rust")
    for name in ("sysroot", "cargo", "ld", "ledger", "compiler-provenance", "target", "cargo-target", "out"):
        rust.add_argument("--" + name, type=Path, required=True)
    prepare_parser = phases.add_parser("prepare")
    link = phases.add_parser("link")
    for phase in (prepare_parser, link):
        phase.add_argument("--tree", type=Path, required=True)
        phase.add_argument("--prefix", required=True)
        phase.add_argument("--out", type=Path, required=True)
    link.add_argument("--language", choices=("c", "rust"), required=True)
    link.add_argument("--baseline", type=Path, required=True)
    link.add_argument("--bundle", type=Path)
    link.add_argument("--trace", action="store_true", help="diagnostic timing wrappers; not a footprint image")
    link.add_argument("--dual-worker", action="store_true", help="same-image C/Rust worker control; requires Rust --trace")
    link.add_argument("--entry-switch", action="store_true", help="same-image C/Rust entry control; requires --dual-worker")
    link.add_argument("--layout-pad-bytes", type=int, default=None,
                      help="diagnostic executable-section padding, 0..2048 bytes in 4-byte steps")
    link.add_argument("--perfmon", action="store_true", help="whole-run hardware counter diagnostic; no event-loop wrappers")
    link.add_argument("--hot-iram", action="store_true", help="diagnostic placement of the shared MQ/poll adapter and workers in IRAM")
    link.add_argument("--faults", action="store_true", help="diagnostic-only shutdown fault fixture; not a footprint/timing image")
    args = parser.parse_args()
    if args.phase == "link" and args.language == "rust" and args.bundle is None:
        parser.error("Rust link requires --bundle")
    if args.phase == "link" and args.dual_worker and (args.language != "rust" or not args.trace):
        parser.error("--dual-worker requires a Rust diagnostic trace image")
    if args.phase == "link" and args.entry_switch and not args.dual_worker:
        parser.error("--entry-switch requires --dual-worker")
    if args.phase == "link" and args.perfmon and args.trace:
        parser.error("--perfmon and --trace are separate diagnostic controls")
    if args.phase == "link" and args.faults and (
            args.trace or args.perfmon or args.hot_iram or args.layout_pad_bytes is not None):
        parser.error("--faults requires a separate uninstrumented-placement diagnostic image")
    if args.phase == "link" and args.layout_pad_bytes is not None and (
            not 0 <= args.layout_pad_bytes <= 2048 or args.layout_pad_bytes % 4 != 0):
        parser.error("--layout-pad-bytes must be between 0 and 2048 and a multiple of 4")
    {"rust": rust_input, "prepare": prepare, "link": final_link}[args.phase](args)


if __name__ == "__main__":
    main()
