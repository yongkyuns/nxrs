#!/usr/bin/env python3
"""Build the pinned, evaluation-only Xtensa Rust compiler in a fresh directory."""

import argparse
import hashlib
import importlib.util
import io
import json
import os
import platform
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
PINS = json.loads((ROOT / "upstream/rust-llvm/upstream.json").read_text())
PATCH_TOOL = ROOT / "tools/apply-nuttx-patches.py"
PATCH_SPEC = importlib.util.spec_from_file_location("nuttx_patch_tool", PATCH_TOOL)
PATCH_MODULE = importlib.util.module_from_spec(PATCH_SPEC)
PATCH_SPEC.loader.exec_module(PATCH_MODULE)
STD_PROPOSAL_TOOL = ROOT / "tools/apply-rust-std-proposals.py"
STD_PROPOSAL_MODULE = None
HOST = "x86_64-unknown-linux-gnu"


def _std_proposal_module():
    """Load the optional std proposal machinery only when a ledger is used."""
    global STD_PROPOSAL_MODULE
    if STD_PROPOSAL_MODULE is None:
        spec = importlib.util.spec_from_file_location(
            "rust_std_proposal_tool", STD_PROPOSAL_TOOL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        STD_PROPOSAL_MODULE = module
    return STD_PROPOSAL_MODULE


def git(source, *args):
    result = subprocess.run(["git", "-C", str(source), *args], text=True,
                           capture_output=True)
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def gitlinks(source, revision):
    links = []
    for entry in git(source, "ls-tree", "-rz", revision).split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, kind, commit = metadata.split()
        if mode == "160000" and kind == "commit":
            links.append((name, commit))
    return links


def submodules(source):
    for name, commit in gitlinks(source, "HEAD"):
        child = source / name
        if not child.is_dir() or child.is_symlink():
            raise ValueError(f"uninitialized submodule: {child}")
        actual = git(child, "rev-parse", "HEAD")
        if actual != commit:
            raise ValueError(f"submodule pin mismatch: {child}")
        if git(child, "status", "--porcelain", "--untracked-files=no"):
            raise ValueError(f"submodule has modified tracked files: {child}")
        submodules(child)


def verify_source(source, revision, label):
    source = Path(source).resolve(strict=True)
    if not source.is_dir() or not (source / ".git").exists():
        raise ValueError(f"{label} input must be a Git checkout")
    actual = git(source, "rev-parse", "HEAD")
    if actual != revision:
        raise ValueError(f"{label} HEAD does not match the pinned revision")
    if git(source, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError(f"{label} has modified tracked files")
    submodules(source)
    return source


def copy_without_git(source, destination):
    shutil.copytree(source, destination, symlinks=True, ignore=shutil.ignore_patterns(".git"))


def extract_archive(data, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        archive.extractall(destination, filter="data")


def export_source(source, destination, revision, runner):
    archive_command = ["git", "-C", str(source), "archive", "--format=tar", revision]
    tree_command = ["git", "-C", str(source), "ls-tree", "-rz", revision]
    if runner.plan:
        runner(archive_command)
        runner(tree_command)
    else:
        runner.record(archive_command)
        data = subprocess.run(archive_command, check=True, capture_output=True).stdout
        extract_archive(data, destination)
        runner.record(tree_command)
    for name, commit in gitlinks(source, revision):
        export_source(source / name, destination / name, commit, runner)
    return destination


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inventory(root):
    rows = []
    for path in sorted(Path(root).rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            target = os.readlink(path)
            resolved = (path.parent / target).resolve(strict=False)
            if not resolved.is_relative_to(Path(root).resolve()):
                raise ValueError(f"packaged source symlink escapes its tree: {relative}")
            rows.append({"path": relative, "symlink": target})
        elif path.is_file():
            rows.append({"path": relative, "sha256": sha256(path)})
    return rows


def validate_std_proposal(snapshot, provenance_path_or_dict):
    """Validate an std RFC ledger against the pinned proposal and library tree.

    ``snapshot`` is the root of the Rust library tree (the directory containing
    ``std/src``). The ledger may be the JSON file emitted by the std proposal
    applicator or an already-loaded provenance dictionary.
    """
    if isinstance(provenance_path_or_dict, dict):
        ledger = provenance_path_or_dict
    else:
        provenance_path = Path(provenance_path_or_dict)
        if provenance_path.is_symlink() or not provenance_path.is_file():
            raise ValueError("std provenance must be a regular JSON file")
        try:
            ledger = json.loads(provenance_path.read_text())
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid std provenance JSON: {error}") from error
    if not isinstance(ledger, dict) or ledger.get("schema") != 1:
        raise ValueError("unsupported std provenance schema")
    if not re.fullmatch(r"[0-9a-f]{64}", str(ledger.get("snapshot_archive_sha256", ""))):
        raise ValueError("invalid std source archive digest in provenance")

    proposal_name = ledger.get("proposal")
    std_proposal_tool = _std_proposal_module()
    selected = std_proposal_tool._load_proposal(proposal_name)
    expected_fields = {
        "proposal_state": selected["state"],
        "qualification": selected["qualification"],
        "selection_scope": "evaluation-preparation-only",
        "target_os": "nuttx",
        "source_revision": PINS["rust_revision"],
        "proposal_manifest_sha256": selected["manifest_sha256"],
    }
    for field, expected in expected_fields.items():
        if (selected.get("source_revision") != PINS["rust_revision"] and
                field == "source_revision"):
            raise ValueError("std proposal Rust source revision does not match the pinned compiler source")
        if ledger.get(field) != expected:
            raise ValueError(f"std provenance {field} does not match the pinned proposal")

    patch_record = ledger.get("patch")
    if patch_record != {"file": selected["patch_name"],
                        "sha256": selected["patch_sha256"]}:
        raise ValueError("std provenance patch does not match its public manifest pin")
    file_records = ledger.get("files")
    expected_paths = set(selected["before_sha256"])
    if not isinstance(file_records, dict) or set(file_records) != expected_paths:
        raise ValueError("std provenance file paths do not match the pinned proposal")

    snapshot = Path(snapshot).resolve(strict=True)
    actual = {}
    for row in inventory(snapshot):
        actual[row["path"]] = row.get("sha256")
    for relative, before in selected["before_sha256"].items():
        std_proposal_tool.safe_source_path(relative)
        record = file_records[relative]
        if (not isinstance(record, dict) or
                set(record) != {"before_sha256", "after_sha256"} or
                record.get("before_sha256") != before):
            raise ValueError(f"std provenance before digest does not match its manifest pin: {relative}")
        after = record.get("after_sha256")
        if not isinstance(after, str) or not re.fullmatch(r"[0-9a-f]{64}", after):
            raise ValueError(f"invalid std provenance after digest: {relative}")
        if actual.get(relative) != after:
            raise ValueError(f"std source after digest does not match provenance: {relative}")

    # Ledger after-digests alone do not prove that the public patch produced
    # these bytes. Reverse the manifest-pinned patch on an isolated copy of
    # only its declared paths, then require the exact pinned preimage hashes.
    with tempfile.TemporaryDirectory(prefix="rust-std-provenance-") as temporary:
        private_root = Path(temporary) / "library"
        private_root.mkdir()
        for relative in expected_paths:
            source = snapshot.joinpath(*relative.split("/"))
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"std proposal source is not a regular file: {relative}")
            private_file = private_root.joinpath(*relative.split("/"))
            private_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, private_file)
        private_patch = Path(temporary) / selected["patch_name"]
        private_patch.write_bytes(selected["patch_bytes"])
        if std_proposal_tool._patch_paths(private_root, private_patch) != expected_paths:
            raise ValueError("public std proposal patch paths do not match the pinned file set")
        reverse_check = subprocess.run(
            ["git", "apply", "--reverse", "--check", "--whitespace=error",
             str(private_patch)], cwd=private_root,
            env=std_proposal_tool._git_env(private_root), text=True, capture_output=True)
        if reverse_check.returncode:
            raise ValueError("std source is not the result of the pinned public patch: " +
                             reverse_check.stderr.strip())
        reverse = subprocess.run(
            ["git", "apply", "--reverse", "--whitespace=error", str(private_patch)],
            cwd=private_root, env=std_proposal_tool._git_env(private_root),
            text=True, capture_output=True)
        if reverse.returncode:
            raise ValueError("could not reverse the pinned std proposal patch: " +
                             reverse.stderr.strip())
        for relative, before in selected["before_sha256"].items():
            private_file = private_root.joinpath(*relative.split("/"))
            if std_proposal_tool.sha256_file(private_file) != before:
                raise ValueError(f"reversed std patch does not match before digest: {relative}")

    return ledger


def install_std_source(package, std_source):
    package = Path(package)
    if package.is_symlink() or not package.is_dir():
        raise ValueError("sysroot package must be a real directory")
    package = package.resolve(strict=True)
    std_source = Path(std_source)
    if std_source.is_symlink():
        raise ValueError("std source must be an independent directory")
    std_source = std_source.resolve(strict=True)
    library = package / "lib/rustlib/src/rust/library"
    relative = library.relative_to(package)
    current = package
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            current.unlink()
            current.mkdir()
        elif current.exists() and not current.is_dir():
            raise ValueError(f"sysroot source path component is not a directory: {current}")
        else:
            current.mkdir(exist_ok=True)
        if not current.resolve(strict=True).is_relative_to(package):
            raise ValueError("sysroot source path escapes the copied package")
    if library.is_symlink():
        library.unlink()
    elif library.is_dir():
        if not library.resolve(strict=True).is_relative_to(package):
            raise ValueError("sysroot library path escapes the copied package")
        shutil.rmtree(library)
    elif library.exists():
        if not library.resolve(strict=True).is_relative_to(package):
            raise ValueError("sysroot library path escapes the copied package")
        library.unlink()
    snapshot = std_source / "library" if (std_source / "library").is_dir() else std_source
    if snapshot.is_symlink():
        raise ValueError("std source library must not be a symlink")
    library.parent.mkdir(parents=True, exist_ok=True)
    copy_without_git(snapshot, library)
    return inventory(library)


def remove_copied_path(package, relative):
    """Remove a path from the copied sysroot, detaching symlink parents first."""
    package = Path(package).resolve(strict=True)
    target = package / relative
    current = package
    for part in Path(relative).parts[:-1]:
        current = current / part
        if current.is_symlink():
            current.unlink()
            current.mkdir()
        elif current.exists() and not current.is_dir():
            raise ValueError(f"sysroot source path component is not a directory: {current}")
        elif not current.exists():
            return
        if not current.resolve(strict=True).is_relative_to(package):
            raise ValueError("sysroot source path escapes the copied package")
    if target.is_symlink():
        target.unlink()
    elif target.is_dir():
        if not target.resolve(strict=True).is_relative_to(package):
            raise ValueError("sysroot source path escapes the copied package")
        shutil.rmtree(target)
    elif target.exists():
        if not target.resolve(strict=True).is_relative_to(package):
            raise ValueError("sysroot source path escapes the copied package")
        target.unlink()


def write_bootstrap_config(path, llvm_build):
    path.write_text(f'''change-id = "ignore"
[build]
build = "{HOST}"
host = ["{HOST}"]
target = ["{HOST}"]
submodules = false
extended = false
tools = []
docs = false
vendor = false
optimized-compiler-builtins = false
[llvm]
download-ci-llvm = false
assertions = true
link-shared = false
[rust]
download-rustc = false
channel = "nightly"
optimize = true
debug-assertions = false
debuginfo-level = 0
incremental = false
codegen-units = 16
lto = "off"
strip = true
backtrace = false
llvm-tools = false
[target.{HOST}]
llvm-config = "{llvm_build / 'bin/llvm-config'}"
llvm-has-rust-patches = true
llvm-filecheck = "{llvm_build / 'bin/FileCheck'}"
''')


class CommandRunner:
    def __init__(self, plan=False):
        self.plan = plan
        self.commands = []

    def __call__(self, argv, cwd=None):
        self.record(argv, cwd)
        if self.plan:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.run(argv, cwd=cwd, check=True, text=True)

    def record(self, argv, cwd=None):
        self.commands.append({"argv": [str(part) for part in argv],
                              "cwd": str(cwd) if cwd else None})
        print(("PLAN " if self.plan else "+ ") +
              (f"(cd {cwd} && " if cwd else "") +
              shlex.join([str(arg) for arg in argv]) +
              (")" if cwd else ""))

    def run_lit(self, argv, expected, suite_label="Xtensa CodeGen/MC"):
        self.record(argv)
        result = subprocess.run(argv, check=True, text=True, capture_output=True)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, argv,
                                                output=result.stdout, stderr=result.stderr)
        print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        output = result.stdout + result.stderr
        if not re.search(rf"^[ \t]*Passed: {expected}(?:\s|$)", output, re.M):
            raise ValueError(f"expected all {expected} {suite_label} tests to pass")
        labels = {
            "Passed": "passed", "Failed": "failed", "Unsupported": "unsupported",
            "Unresolved": "unresolved", "Skipped": "skipped",
            "Expectedly Failed": "expectedly_failed",
            "Unexpectedly Passed": "unexpectedly_passed",
        }
        counts = {key: 0 for key in labels.values()}
        for label, key in labels.items():
            matches = re.findall(rf"^[ \t]*{re.escape(label)}:\s*(\d+)(?:\s|$)",
                                 output, re.M)
            if matches:
                counts[key] = int(matches[-1])
        return counts


def build(args, runner=None):
    proposal_set = getattr(args, "proposal_set", None)
    proposal_metadata = PATCH_MODULE.proposal_metadata(
        "rust-llvm", proposal_set, PINS["qualification_tests"]["total"])
    expected_tests = proposal_metadata["qualification_tests"]
    extra_test_suites = [
        {"paths": list(suite["paths"]),
         "expected_passes": suite["tests"],
         "count_scope": "llvm-lit Passed count"}
        for suite in proposal_metadata["extra_test_suites"]
    ]
    runner = runner or CommandRunner(args.plan)
    if not args.plan and (platform.system() != "Linux" or platform.machine().lower() not in
                          ("x86_64", "amd64")):
        raise ValueError("compiler evaluation build requires Linux x86_64")
    llvm_source = verify_source(args.llvm_source, PINS["revision"], "LLVM")
    rust_source = verify_source(args.rust_source, PINS["rust_revision"], "Rust")
    std_input = Path(args.std_source).expanduser().absolute()
    if std_input.is_symlink():
        raise ValueError("std source must be an independent directory")
    std_source = std_input.resolve(strict=True)
    if not std_source.is_dir():
        raise ValueError("std source must be an independent directory")
    snapshot = std_source / "library" if (std_source / "library").is_dir() else std_source
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise ValueError("std source snapshot must be a real independent directory")
    core_source = snapshot / "core/src/lib.rs"
    if (core_source.is_symlink() or not core_source.is_file() or
            not core_source.resolve(strict=True).is_relative_to(snapshot.resolve(strict=True))):
        raise ValueError("std snapshot core/src/lib.rs must be a regular file within the snapshot")
    core_source_sha256 = sha256(core_source)
    std_provenance_path = getattr(args, "std_provenance", None)
    std_proposal_ledger = None
    std_input_inventory = None
    if std_provenance_path is not None:
        std_proposal_ledger = validate_std_proposal(snapshot, std_provenance_path)
        # Bind the checked input tree to the inventory later recorded for the
        # packaged library, so unrelated files cannot drift during packaging.
        std_input_inventory = inventory(snapshot)
        input_hashes = {row["path"]: row.get("sha256") for row in std_input_inventory}
        for relative, record in std_proposal_ledger["files"].items():
            if input_hashes.get(relative) != record["after_sha256"]:
                raise ValueError(f"std input inventory does not match provenance: {relative}")

    output = Path(args.out).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("output must be fresh; it already exists")
    output = output.parent.resolve() / output.name
    for source in (llvm_source, rust_source, std_source):
        if output == source or output in source.parents or source in output.parents:
            raise ValueError("output and input trees must be separate")
    for source in (llvm_source, rust_source):
        if std_source == source or std_source in source.parents or source in std_source.parents:
            raise ValueError("std source must be independent of the pinned compiler checkouts")

    if args.plan:
        print(f"Pinned LLVM {PINS['revision']} and Rust {PINS['rust_revision']}")
        print(f"Proposal set: {proposal_set or 'default'}; expected Xtensa tests: {expected_tests}")
        print(f"Would export verified inputs under {output}")

    llvm = output / "llvm-source"
    rust = output / "rust-source"
    llvm_build = output / "llvm-build"
    rust_build = rust / "build"
    if not args.plan:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.mkdir()
    export_source(llvm_source, llvm, PINS["revision"], runner)
    export_source(rust_source, rust, PINS["rust_revision"], runner)
    record = output / "llvm-patches.json"
    patch_command = [sys.executable, PATCH_TOOL, "--component", "rust-llvm",
                     "--source", llvm, "--revision", PINS["revision"], "--record", record]
    if proposal_set is not None:
        patch_command.extend(("--proposal-set", proposal_set))
    runner(patch_command)
    cmake = ["cmake", "-S", llvm / "llvm", "-B", llvm_build, "-G", "Ninja",
             "-DLLVM_TARGETS_TO_BUILD=X86", "-DLLVM_EXPERIMENTAL_TARGETS_TO_BUILD=Xtensa",
             "-DLLVM_ENABLE_ASSERTIONS=ON",
             "-DLLVM_BUILD_LLVM_DYLIB=OFF", "-DLLVM_LINK_LLVM_DYLIB=OFF",
             "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_C_FLAGS_RELEASE=-O1",
             "-DCMAKE_CXX_FLAGS_RELEASE=-O1", "-DLLVM_INCLUDE_TESTS=ON",
             "-DLLVM_BUILD_TOOLS=ON"]
    runner(cmake)
    runner(["cmake", "--build", llvm_build, "--parallel", str(args.jobs)])
    lit = llvm_build / "bin/llvm-lit"
    lit_command = [lit, "-sv", llvm / "llvm/test/CodeGen/Xtensa",
                   llvm / "llvm/test/MC/Xtensa"]
    if args.plan or not hasattr(runner, "run_lit"):
        runner(lit_command)
    else:
        runner.run_lit(lit_command, expected_tests)
    for suite in extra_test_suites:
        suite_paths = [llvm / path for path in suite["paths"]]
        suite_command = [lit, "-sv", *suite_paths]
        if args.plan or not hasattr(runner, "run_lit"):
            runner(suite_command)
        else:
            suite["result"] = runner.run_lit(
                suite_command, suite["expected_passes"], suite_label="extra LLVM lit suite")
    bootstrap_config = rust / "bootstrap.toml"
    if not args.plan:
        write_bootstrap_config(bootstrap_config, llvm_build)
    runner(["python3", "x.py", "build", "--stage", "1", "compiler/rustc",
            "library", "--jobs", str(args.jobs)], cwd=rust)
    if args.plan:
        print("Plan complete; no output files were created")
    else:
        stage1 = rust_build / HOST / "stage1"
        if not stage1.is_dir():
            raise ValueError(f"Rust stage 1 sysroot missing: {stage1}")
        rustc_input = stage1 / "bin/rustc"
        driver_libraries = sorted((stage1 / "lib").glob("librustc_driver-*.so"))
        if not rustc_input.is_file():
            raise ValueError("Rust stage 1 compiler is missing")
        if not driver_libraries:
            raise ValueError("Rust stage 1 librustc_driver shared library is missing")
        package = output / "sysroot"
        copy_without_git(stage1, package)
        remove_copied_path(package, Path("lib/rustlib/rustc-src/rust"))
        std_inventory = install_std_source(package, std_source)
        if std_proposal_ledger is not None and std_inventory != std_input_inventory:
            raise ValueError("packaged std inventory differs from the validated provenance input")
        if std_proposal_ledger is not None:
            packaged_hashes = {row["path"]: row.get("sha256") for row in std_inventory}
            for relative, record in std_proposal_ledger["files"].items():
                if packaged_hashes.get(relative) != record["after_sha256"]:
                    raise ValueError(f"packaged std after digest does not match provenance: {relative}")
        validate_tree_symlinks(package)
        packaged_core = package / "lib/rustlib/src/rust/library/core/src/lib.rs"
        if sha256(packaged_core) != core_source_sha256:
            raise ValueError("packaged core source differs from the independent snapshot")
        rustc = package / "bin/rustc"
        version_argv = [str(rustc), "-vV"]
        runner.commands.append({"argv": version_argv, "cwd": None})
        version = subprocess.run(version_argv, check=True, text=True,
                                 capture_output=True).stdout
        binaries = {"rustc": sha256(rustc)}
        packaged_drivers = sorted((package / "lib").glob("librustc_driver-*.so"))
        if not packaged_drivers:
            raise ValueError("packaged librustc_driver shared library is missing")
        patch_ledger = json.loads(record.read_text())
        provenance = {
            "schema": 1, "status": PINS["status"],
            "llvm_revision": PINS["revision"], "rust_revision": PINS["rust_revision"],
            "proposal_set": proposal_set, "qualification_tests": expected_tests,
            "extra_test_suites": extra_test_suites,
            "patch_ledger": patch_ledger, "commands": runner.commands,
            "rustc_vV": version, "compiler_binaries_sha256": binaries,
            "rustc_driver_library_sha256": {path.name: sha256(path)
                                             for path in packaged_drivers},
            "llvm_tools_sha256": {name: sha256(llvm_build / "bin" / name)
                                   for name in ("llc", "opt", "llvm-config")
                                   if (llvm_build / "bin" / name).is_file()},
            "std_source_inventory": std_inventory,
            "core_src_sha256": core_source_sha256,
            "std_source_inventory_sha256": hashlib.sha256(json.dumps(
                std_inventory, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        }
        if std_proposal_ledger is not None:
            provenance["std_proposal_ledger"] = std_proposal_ledger
        (output / "build-provenance.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    return output


def validate_tree_symlinks(root):
    root = Path(root).resolve()
    for path in root.rglob("*"):
        if path.is_symlink():
            resolved = path.resolve(strict=False)
            if not resolved.is_relative_to(root):
                raise ValueError(f"packaged sysroot symlink escapes its tree: {path.relative_to(root)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llvm-source", type=Path, required=True)
    parser.add_argument("--rust-source", type=Path, required=True)
    parser.add_argument("--std-source", type=Path, required=True,
                        help="independent library source tree containing core/src/lib.rs")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--plan", action="store_true", help="validate inputs and print build steps")
    parser.add_argument("--proposal-set", help="opt in to a named Rust LLVM proposal set")
    parser.add_argument("--std-provenance", type=Path,
                        help="optional ledger from apply-rust-std-proposals.py")
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    try:
        build(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"build-rust-llvm: {error}\n")


if __name__ == "__main__":
    main()
