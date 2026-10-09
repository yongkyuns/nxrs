#!/usr/bin/env python3
"""Build a native Rust input, then link both controls in one private NuttX tree.

Explicit paths keep this usable with the opt-in compiler package on Linux and
the local SDK final link. This never installs a compiler or flashes a device.
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
import kernel_evidence

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
STD_FEATURES = ["backtrace-trace-only", "optimize_for_size", "panic_immediate_abort"]


def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(argv, log, cwd=None, env=None):
    with log.open("w") as stream:
        subprocess.run(list(map(str, argv)), cwd=cwd, env=env, stdout=stream,
                       stderr=subprocess.STDOUT, check=True)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_generated(directory):
    record = json.loads((directory / "coverage.json").read_text())
    if record["schema"] != 1 or not record["cases"]:
        raise ValueError("invalid arithmetic coverage")
    for name, expected in record["artifacts"].items():
        if Path(name).name != name or digest(directory / name) != expected:
            raise ValueError(f"generated artifact changed: {name}")
    for name, expected in record["sources"].items():
        if Path(name).name != name or digest(HERE / name) != expected:
            raise ValueError(f"qualification source changed: {name}; generate a fresh corpus")
    return record


def validate_compiler(sysroot, inventory, ledger, provenance_path):
    """Bind the ledger to the actual compiler package, not just its patch list."""
    package = json.loads(provenance_path.read_text())
    recorded = {row["path"]: row["sha256"] for row in package["std_source_inventory"]}
    drivers = {path.name: digest(path) for path in (sysroot / "lib").glob("librustc_driver*.so")}
    if not drivers or drivers != package["rustc_driver_library_sha256"]:
        raise ValueError("compiler driver does not match the qualified package")
    if digest(sysroot / "bin/rustc") != package["compiler_binaries_sha256"]["rustc"]:
        raise ValueError("rustc does not match the qualified package")
    if inventory != recorded or ledger != package["patch_ledger"]:
        raise ValueError("std snapshot/patch ledger does not match the qualified package")
    if "std_proposal_ledger" in package:
        compiler_builder = load("aq_compiler_builder", ROOT / "tools/build-rust-llvm.py")
        compiler_builder.validate_std_proposal(
            sysroot / "lib/rustlib/src/rust/library", package["std_proposal_ledger"])
    return package


def rust_input(args):
    generated, out, sysroot = args.generated.resolve(), args.out.resolve(), args.sysroot.resolve()
    validate_generated(generated)
    out.mkdir(parents=True, exist_ok=False)
    rustc, cargo = sysroot / "bin/rustc", args.cargo.resolve()
    library = sysroot / "lib/rustlib/src/rust/library"
    inventory = {str(path.relative_to(library)): digest(path) for path in sorted(library.rglob("*")) if path.is_file()}
    if len(inventory) < 1000: raise ValueError("complete qualified embedded std snapshot required")
    ledger = json.loads(args.ledger.read_text())
    package = validate_compiler(sysroot, inventory, ledger, args.compiler_provenance)
    wrapper = ROOT / "tests/nuttx-std/link.py"
    keep = json.loads((generated / "keep-symbols.json").read_text())
    if any(not symbol.startswith("aq_r_") or not symbol[5:].isdigit() for symbol in keep):
        raise ValueError("invalid retained Rust kernel symbol")
    flags = "-C panic=abort -C linker=" + str(wrapper)
    flags += "".join(" -C link-arg=-u" + symbol for symbol in keep)
    env = dict(os.environ, RUSTC_BOOTSTRAP="1", RUSTUP_TOOLCHAIN="1.90.0",
               RUSTC=str(rustc), RUSTDOC=str(sysroot / "bin/rustdoc"),
               NUTTX_STD_SYSROOT=str(sysroot), NUTTX_STD_GNU_LINKER=str(args.ld.resolve()),
               NUTTX_STD_LINK_LOG=str(out / "rust-link.json"),
               CARGO_TARGET_DIR=str(args.cargo_target.resolve()), RUSTFLAGS=flags)
    command = [cargo, "--config", "build.jobs=1", "rustc", "--release", "--offline", "--locked",
               "--target", args.target.resolve(), "-Zbuild-std=std,panic_abort",
               "-Zbuild-std-features=" + ",".join(STD_FEATURES),
               "--message-format=json-render-diagnostics", "--", "--emit=llvm-ir,asm,link"]
    # Std source is selected by Cargo, not inferred from compiler --sysroot.
    with (out / "cargo-messages.jsonl").open("w") as stream, (out / "cargo-build.log").open("w") as log:
        subprocess.run(list(map(str, command)), cwd=generated, env=env, stdout=stream, stderr=log, check=True)
    messages = [json.loads(line) for line in (out / "cargo-messages.jsonl").read_text().splitlines()]
    standard = [row for row in messages if row.get("reason") == "compiler-artifact" and row["target"]["name"] == "std"]
    binary = [row for row in messages if row.get("reason") == "compiler-artifact" and row["target"]["name"] == "arithmetic-parity"]
    if len(standard) != 1 or Path(standard[0]["target"]["src_path"]).resolve() != (library / "std/src/lib.rs").resolve():
        raise ValueError("Cargo did not select the qualified std snapshot")
    if len(binary) != 1 or binary[0]["profile"]["opt_level"] != "2":
        raise ValueError("Cargo arithmetic optimization policy differs")
    if not (out / "rust-input.elf").exists(): shutil.copy2(binary[0]["executable"], out / "rust-input.elf")
    before = digest(out / "rust-input.elf")
    if inventory != {str(path.relative_to(library)): digest(path) for path in sorted(library.rglob("*")) if path.is_file()}:
        raise ValueError("std snapshot changed during compilation")
    pins = json.loads((ROOT / "upstream/rust-llvm/upstream.json").read_text())
    if ledger["upstream_revision"] != pins["revision"] or ledger["rust_revision"] != pins["rust_revision"]:
        raise ValueError("compiler patch ledger source pins differ")
    for patch in ledger["patches"]:
        if Path(patch["name"]).name != patch["name"]:
            raise ValueError("invalid compiler patch name")
        folder = "proposals" if patch.get("proposal") else "patches"
        if digest(ROOT / "upstream/rust-llvm" / folder / patch["name"]) != patch["sha256"]:
            raise ValueError("compiler patch bytes differ")
    record = dict(schema=1, input_elf_sha256=before, coverage_sha256=digest(generated / "coverage.json"),
                  target_sha256=digest(args.target), link_wrapper_sha256=digest(wrapper),
                  compiler_sha256=digest(rustc), compiler_version=subprocess.check_output([rustc,"-vV"],text=True),
                  compiler_libraries_sha256={str(path.relative_to(sysroot)):digest(path) for path in (sysroot / "lib").glob("librustc_driver*.so")},
                  cargo_sha256=digest(cargo), partial_linker_sha256=digest(args.ld),
                  compiler_package_provenance_sha256=digest(args.compiler_provenance),
                  compiler_qualification_tests=package["qualification_tests"],
                  std_inventory_sha256=hashlib.sha256(json.dumps(inventory,sort_keys=True,separators=(",",":")).encode()).hexdigest(),
                  std_features=STD_FEATURES, application_opt_level="2", patch_ledger=ledger)
    if "std_proposal_ledger" in package:
        record["std_proposal_ledger"] = package["std_proposal_ledger"]
    (out / "compiler-input.json").write_text(json.dumps(record,indent=2)+"\n")
    print("ARITHMETIC_RUST_INPUT_PASS", before)


def final_link(args):
    generated, bundle, tree, out = (path.resolve() for path in (args.generated,args.bundle,args.tree,args.out))
    coverage = validate_generated(generated)
    proof = json.loads((bundle / "compiler-input.json").read_text())
    if proof["coverage_sha256"] != digest(generated / "coverage.json") or proof["input_elf_sha256"] != digest(bundle / "rust-input.elf"):
        raise ValueError("Rust input/coverage identities differ")
    out.mkdir(parents=True,exist_ok=False)
    helpers = load("aq_relink", ROOT / "tests/service-footprint/relink_rust.py")
    inventory_helper = kernel_evidence
    # The tree is an explicitly supplied, isolated, already-built SDK archive.
    lock = helpers.lock_build_tree(tree)
    prefix = str(args.prefix)
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(Path(prefix).parent),str(ROOT / "target/zephyr-python/bin"),
                                  "/usr/local/opt/gnu-sed/libexec/gnubin",env.get("PATH","")])
    config = tree / "nuttx/.config"
    for option, state in (("CONFIG_EXAMPLES_NXRS_STD_APP","enable"),("CONFIG_EXAMPLES_NXRS_BENCH","disable")):
        subprocess.run(["kconfig-tweak","--"+state,option],cwd=tree/"nuttx",env=env,check=True)
    before_config = digest(config)
    libraries = inventory_helper.kernel_inventory(tree,prefix)
    if not libraries: raise ValueError("prepared NuttX kernel archives missing")
    (out / "kernel-inputs-before.json").write_text(json.dumps(libraries,indent=2)+"\n")
    if digest(tree / "xtensa-esp32s3-nuttx.json") != proof["target_sha256"]:
        raise ValueError("final link target specification differs")
    app = tree / "apps/examples/nxrs_std_app"
    names = []
    for index, source in enumerate((HERE / "driver.c",generated / "kernels.c",generated / "vectors.c")):
        name = f"aq_source_{index}.c"
        shutil.copy2(source,app / name);names.append(name)
    shutil.copy2(HERE / "contract.h",app / "contract.h")
    uname_before = inventory_helper.freeze_utsname(tree,prefix,out,"uname-before.o")
    command = ["make","-j2","--old-file=arch/xtensa/src/.context","CROSSDEV="+Path(prefix).name,
               "NXRS_STD_ELF="+str(bundle / "rust-input.elf"),"NXRS_APP_COMMAND=arithmetic_suite",
               "NXRS_APP_PRIORITY=100","NXRS_APP_STACKSIZE=8192",
               "NXRS_TARGET_C_SOURCE="+" ".join(names),
               "NXRS_TARGET_C_FLAGS=-std=c11 -O2 -ffp-contract=off -fno-fast-math -fno-math-errno",
               "ESPTOOL_BINDIR=."]
    with (out / "make.log").open("w") as log:
        helpers.refresh_registration(tree,command[2:],env,log)
        subprocess.run(command,cwd=tree/"nuttx",env=env,stdout=log,stderr=subprocess.STDOUT,check=True)
    after_libraries = inventory_helper.kernel_inventory(tree,prefix)
    (out / "kernel-inputs-after.json").write_text(json.dumps(after_libraries,indent=2)+"\n")
    if digest(config) != before_config or libraries != after_libraries:
        changed = sorted(name for name in set(libraries)|set(after_libraries)
                         if libraries.get(name) != after_libraries.get(name))
        raise ValueError(f"kernel inputs changed during arithmetic final link: {changed}")
    inventory_helper.timestamp_only(uname_before,inventory_helper.freeze_utsname(tree,prefix,out,"uname-after.o"),prefix)
    for src,dest in (("nuttx","app.elf"),("nuttx.bin","image.bin"),(".config","resolved.config")):
        shutil.copy2(tree/"nuttx"/src,out/dest)
    run([prefix+"objdump","-d",out/"app.elf"],out/"disassembly.txt")
    nm = subprocess.check_output([prefix+"nm","-S","--defined-only",out/"app.elf"],text=True)
    symbols = {}
    for line in nm.splitlines():
        fields=line.split()
        if len(fields)==4 and fields[3].startswith(("aq_c_","aq_r_")):
            symbols[fields[3]]=dict(address=int(fields[0],16),function_bytes=int(fields[1],16))
    for case in coverage["cases"]:
        for name in (case["c_symbol"],case["rust_symbol"]):
            if name is not None and name not in symbols: raise ValueError(f"missing final arithmetic symbol: {name}")
    record=dict(schema=1,compiler_input=proof,coverage_sha256=proof["coverage_sha256"],
                kernel_config_sha256=before_config,kernel_libraries_sha256=libraries,
                c_compiler_sha256=digest(prefix+"gcc"),c_compiler_version=subprocess.check_output([prefix+"gcc","--version"],text=True).splitlines()[0],
                c_flags=command[-2],symbols=symbols,
                source_sha256={name:digest(app/name) for name in [*names,"contract.h"]},
                artifacts={name:digest(out/name) for name in ("app.elf","image.bin","resolved.config")},
                size_scope="Per-function bytes exclude shared helpers/literals; combined diagnostic image is not a C/Rust firmware size delta")
    (out/"build-provenance.json").write_text(json.dumps(record,indent=2)+"\n")
    lock.close()
    print("ARITHMETIC_FINAL_LINK_PASS",out)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    subs=parser.add_subparsers(dest="phase",required=True)
    rust=subs.add_parser("rust")
    for arg in ("generated","sysroot","cargo","target","ld","ledger","compiler-provenance","cargo-target","out"):
        rust.add_argument("--"+arg,type=Path,required=True)
    link=subs.add_parser("link")
    for arg in ("generated","bundle","tree","out"):
        link.add_argument("--"+arg,type=Path,required=True)
    link.add_argument("--prefix",required=True)
    args=parser.parse_args()
    (rust_input if args.phase=="rust" else final_link)(args)


if __name__=="__main__":main()
