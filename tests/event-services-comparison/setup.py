#!/usr/bin/env python3
"""Opt-in, isolated Zephyr/Embassy setup; never flashes or enables LLVM patches."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PIN_FILE = HERE / "setup-pins.json"
PINS = json.loads(PIN_FILE.read_text())
TRIPLE = "xtensa-espressif_esp32s3_zephyr-elf"
TARGET = "xtensa-esp32s3-none-elf"
spec = importlib.util.spec_from_file_location("setup_zephyr", HERE.parent / "zephyr-comparison/build.py")
zephyr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zephyr)


def host_key(system, machine):
    os_name = {"Darwin": "macos", "Linux": "linux"}.get(system)
    arch = {"x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(machine)
    if not os_name or not arch:
        raise ValueError("supported hosts: Linux/macOS on x86_64 or aarch64")
    return f"{os_name}-{arch}"


def selected_platforms(name):
    if name not in ("zephyr", "embassy", "all"):
        raise ValueError("select zephyr, embassy or all")
    return ("zephyr", "embassy") if name == "all" else (name,)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(argv, cwd=ROOT, env=None, capture=False):
    argv = list(map(str, argv))
    print("+", shlex.join(argv), flush=True)
    result = subprocess.run(argv, cwd=cwd, env=env, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def download(url, sha256, destination):
    """Never publish an unverified archive, including on cache hits."""
    if not url.startswith("https://"):
        raise ValueError("downloads require HTTPS")
    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError(f"refusing symlink cache entry: {destination}")
    if destination.exists():
        if digest(destination) != sha256:
            raise ValueError(f"SHA256 mismatch in cache: {destination}; preserve it and use a fresh --root")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".download-", dir=destination.parent) as temporary:
        partial = Path(temporary) / "archive"
        print("Downloading", url, flush=True)
        with urllib.request.urlopen(url, timeout=60) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output)
        if digest(partial) != sha256:
            raise ValueError(f"SHA256 mismatch: {url}")
        partial.replace(destination)
    return destination


def extract(archive, destination, omit=()):
    """Stage extraction so a failed or malicious archive cannot poison a retry."""
    archive, destination = Path(archive), Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"refusing existing extraction destination: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".extract-", dir=destination.parent) as temporary:
        stage = Path(temporary) / "tree"
        stage.mkdir()
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    if not (stage / member.filename).resolve().is_relative_to(stage.resolve()):
                        raise ValueError("ZIP path traversal")
                    mode = member.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        raise ValueError("ZIP symlinks are not supported")
                    path = Path(bundle.extract(member, stage))
                    if not member.is_dir() and mode & 0o111:
                        path.chmod(path.stat().st_mode | 0o111)
        else:
            try:
                with tarfile.open(archive) as bundle:
                    def selected_data(member, path):
                        parts = Path(member.name).parts
                        if len(parts) > 1 and parts[1] in omit:
                            return None
                        return tarfile.data_filter(member, path)
                    bundle.extractall(stage, filter=selected_data)
            except tarfile.FilterError as exc:
                raise ValueError(f"unsafe tar archive: {exc}") from exc
        stage.rename(destination)


def checkout(destination, url, revision, tag=None):
    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError(f"refusing symlink checkout: {destination}")
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".checkout-", dir=destination.parent) as temporary:
            stage = Path(temporary) / "tree"
            run(["git", "init", "-q", stage])
            run(["git", "remote", "add", "origin", url], cwd=stage)
            ref = f"refs/tags/{tag}:refs/tags/{tag}" if tag else revision
            run(["git", "fetch", "--depth", "1", "origin", ref], cwd=stage)
            run(["git", "checkout", "--detach", revision], cwd=stage)
            stage.rename(destination)
    if (Path(run(["git", "rev-parse", "--show-toplevel"], cwd=destination, capture=True)).resolve()
            != destination.resolve()):
        raise ValueError(f"not an independent checkout: {destination}")
    if run(["git", "remote", "get-url", "origin"], cwd=destination, capture=True) != url:
        raise ValueError(f"checkout origin mismatch: {destination}")
    if run(["git", "status", "--porcelain"], cwd=destination, capture=True):
        raise ValueError(f"dirty checkout; preserving local changes: {destination}")
    if run(["git", "rev-parse", "HEAD"], cwd=destination, capture=True) != revision:
        raise ValueError(f"checkout revision mismatch: {destination}")


def assets(host, platforms):
    names = []
    if "zephyr" in platforms:
        names += ["sdk", "zephyr-toolchain"]
    if "embassy" in platforms:
        names += ["gcc", "rust", "rust-src", "espflash"]
    selected = {}
    for name in names:
        release, filename, sha = PINS["common"].get(name) or PINS["hosts"][host][name]
        selected[name] = {"url": PINS["releases"][release] + filename, "sha256": sha,
                          "filename": filename}
    return selected


def package(root, name, pin):
    archive = download(pin["url"], pin["sha256"], root / "downloads" / pin["filename"])
    destination = root / "packages" / name
    marker = destination / ".nxrs-archive-sha256"
    if destination.is_symlink() or marker.is_symlink():
        raise ValueError(f"refusing symlink package: {destination}")
    if destination.exists():
        if not marker.is_file() or marker.read_text().strip() != pin["sha256"]:
            raise ValueError(f"unmanaged or incomplete package: {destination}; use a fresh --root")
    else:
        omit = ("rust-docs", "rust-docs-json-preview", "rustfmt-preview", "clippy-preview") if name == "rust" else ()
        extract(archive, destination, omit=omit)
        marker.write_text(pin["sha256"] + "\n")
    return destination


def sdk_path(root):
    return root / "packages/sdk/zephyr-sdk-0.17.0"


def environment(root, embassy=False):
    env = os.environ.copy()
    # A previously sourced NuttX toolchain must not silently override this SDK.
    for key in ("RUSTC", "RUSTDOC", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS",
                "RUSTUP_TOOLCHAIN", "CARGO_BUILD_TARGET", "ZEPHYR_BASE", "ZEPHYR_TOOLCHAIN_VARIANT"):
        env.pop(key, None)
    paths = [str(root / "venv/bin")]
    if embassy:
        env.update(RUSTUP_HOME=str(root / "rustup"), CARGO_HOME=str(root / "cargo"))
        paths += [str(root / "packages/gcc/xtensa-esp-elf/bin")]
    env["PATH"] = os.pathsep.join(paths + [env.get("PATH", "")])
    env.update(PIP_CONFIG_FILE=os.devnull, PIP_REQUIRE_VIRTUALENV="true",
               PIP_CACHE_DIR=str(root / "pip-cache"), CCACHE_DIR=str(root / "ccache"))
    return env


def prepare_root(root, host):
    if root.is_symlink() or root.resolve() in (Path("/"), Path.home(), ROOT):
        raise ValueError("choose an isolated setup directory")
    root = root.resolve()
    if root.is_relative_to(ROOT) and not root.is_relative_to(ROOT / "target"):
        raise ValueError("in-repository dependencies must stay under ignored target/")
    marker = root / ".nxrs-comparison.json"
    for name in (".nxrs-comparison.json", ".setup.lock", ".python-platforms.json",
                 "python-resolved.txt", "environment.sh", "sources", "packages", "downloads",
                 "venv", "rustup", "cargo", "pip-cache", "ccache", "rust-toolchain", "cargo-target"):
        if (root / name).is_symlink():
            raise ValueError(f"refusing symlink setup state: {root / name}")
    identity = {"schema": 1, "host": host, "pins_sha256": digest(PIN_FILE)}
    if root.exists() and any(root.iterdir()):
        if not marker.is_file() or json.loads(marker.read_text()) != identity:
            raise ValueError(f"unmanaged directory or changed pins: {root}; use a fresh --root")
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(identity, indent=2) + "\n")
    return root


def prepare_zephyr(root, selected):
    sources = root / "sources"
    checkout(sources / "zephyr", "https://github.com/zephyrproject-rtos/zephyr.git",
             zephyr.ZEPHYR_SHA, tag="v4.3.1")
    for name in ("espressif", "xtensa"):
        checkout(sources / f"hal_{name}", f"https://github.com/zephyrproject-rtos/hal_{name}.git",
                 zephyr.PINS[name])
    package(root, "sdk", selected["sdk"])
    toolchain = package(root, "zephyr-toolchain", selected["zephyr-toolchain"]) / TRIPLE
    link = sdk_path(root) / TRIPLE
    if not link.exists() and not link.is_symlink():
        link.symlink_to(toolchain, target_is_directory=True)
    if link.resolve() != toolchain.resolve():
        raise ValueError("unexpected Zephyr toolchain link")
    if (sdk_path(root) / "sdk_version").read_text().strip() != "0.17.0":
        raise ValueError("Zephyr SDK version mismatch")
    run([link / "bin" / (TRIPLE + "-gcc"), "--version"])


def prepare_python(root, platforms):
    python = root / "venv/bin/python"
    if not python.exists():
        run([sys.executable, "-m", "venv", root / "venv"])
    marker = root / ".python-platforms.json"
    installed = set(json.loads(marker.read_text())) if marker.exists() else set()
    env = environment(root)
    if not installed:
        run([python, "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
             "--only-binary=cryptography,cffi",
             "esptool==5.3.0"], env=env)
    if "zephyr" in platforms and "zephyr" not in installed:
        run([python, "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
             "--only-binary=cryptography,cffi",
             "cmake==3.31.6", "ninja==1.13.0", "-r",
             root / "sources/zephyr/scripts/requirements-base.txt"], env=env)
    run([python, "-m", "pip", "check"], env=env)
    marker.write_text(json.dumps(sorted(installed | set(platforms))) + "\n")
    freeze = run([python, "-m", "pip", "freeze"], env=env, capture=True)
    (root / "python-resolved.txt").write_text(freeze + "\n")


def prepare_embassy(root, selected):
    for name in ("gcc", "rust", "rust-src", "espflash"):
        package(root, name, selected[name])
    env = environment(root, embassy=True)
    toolchain = root / "rust-toolchain"
    marker = toolchain / ".nxrs-installed"
    if not marker.exists():
        if toolchain.exists():
            raise ValueError(f"incomplete Rust installation: {toolchain}; use a fresh --root")
        installer, = (root / "packages/rust").glob("*/install.sh")
        components = (installer.parent / "components").read_text().splitlines()
        host_std, = (name for name in components if name.startswith("rust-std-"))
        run(["bash", installer, f"--prefix={toolchain}", "--disable-ldconfig",
             f"--components=rustc,{host_std},cargo"], env=env)
        core_manifest, = (p for p in (root / "packages/rust-src").rglob("Cargo.toml")
                          if p.as_posix().endswith("/library/core/Cargo.toml"))
        library = core_manifest.parent.parent
        source = toolchain / "lib/rustlib/src/rust"
        source.mkdir(parents=True, exist_ok=True)
        (source / "library").symlink_to(library, target_is_directory=True)
        run(["rustup", "toolchain", "link", "esp", toolchain], env=env)
        marker.write_text("ESP Rust/Cargo distribution 1.90.0.0\n")
    run(["rustc", "+esp", "--version", "--verbose"], env=env)
    run([root / "packages/gcc/xtensa-esp-elf/bin/xtensa-esp32s3-elf-gcc", "--version"], env=env)
    run([root / "packages/espflash/espflash", "--version"], env=env)
    # Seed both app locks and build-std's lock for the offline event builder.
    manifests = [HERE / "Cargo.toml", HERE.parent / "embassy-comparison/Cargo.toml",
                 toolchain / "lib/rustlib/src/rust/library/Cargo.toml"]
    for manifest in manifests:
        run(["cargo", "+esp", "fetch", "--locked", "--target", TARGET,
             "--manifest-path", manifest], cwd=manifest.parent, env=env)


def build_command(platform_name, root, out, layout="three"):
    if platform_name not in ("zephyr", "embassy"):
        raise ValueError("build supports zephyr or embassy")
    argv = [sys.executable, HERE / "build.py", "--platform",
            "zephyr-c" if platform_name == "zephyr" else "embassy",
            "--layout", layout, "--timer-ms", "1", "--out", out]
    if platform_name == "zephyr":
        argv += ["--zephyr", root / "sources/zephyr", "--espressif", root / "sources/hal_espressif",
                 "--xtensa", root / "sources/hal_xtensa", "--sdk", sdk_path(root),
                 "--zephyr-python", root / "venv/bin/python", "--readelf",
                 sdk_path(root) / TRIPLE / "bin" / (TRIPLE + "-readelf")]
    else:
        argv += ["--espflash", root / "packages/espflash/espflash", "--readelf",
                 root / "packages/gcc/xtensa-esp-elf/bin/xtensa-esp32s3-elf-readelf",
                 "--cargo-target", root / "cargo-target", "--embassy-scheduling", "natural"]
    return list(map(str, argv))


def write_environment(root, platforms):
    values = {"ZEPHYR_PYTHON": root / "venv/bin/python", "ESPTOOL": root / "venv/bin/esptool",
              "PIP_CACHE_DIR": root / "pip-cache", "CCACHE_DIR": root / "ccache"}
    paths = [root / "venv/bin"]
    if "zephyr" in platforms:
        values.update(ZEPHYR_SRC=root / "sources/zephyr", ESPRESSIF_HAL=root / "sources/hal_espressif",
                      XTENSA_HAL=root / "sources/hal_xtensa", ZEPHYR_SDK=sdk_path(root))
    if "embassy" in platforms:
        values.update(RUSTUP_HOME=root / "rustup", CARGO_HOME=root / "cargo",
                      ESPFLASH=root / "packages/espflash/espflash",
                      READELF=root / "packages/gcc/xtensa-esp-elf/bin/xtensa-esp32s3-elf-readelf")
        paths += [root / "packages/gcc/xtensa-esp-elf/bin"]
    lines = ["# Optional comparison environment; no global toolchain registration.",
             "unset RUSTC RUSTDOC RUSTFLAGS CARGO_ENCODED_RUSTFLAGS RUSTUP_TOOLCHAIN CARGO_BUILD_TARGET"]
    lines += [f"export {key}={shlex.quote(str(value))}" for key, value in values.items()]
    lines += ["export PATH=" + shlex.quote(os.pathsep.join(map(str, paths))) + ':"$PATH"']
    (root / "environment.sh").write_text("\n".join(lines) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=("zephyr", "embassy", "all"))
    parser.add_argument("--root", type=Path, default=ROOT / "target/rtos-comparison")
    parser.add_argument("--plan", action="store_true", help="print pins and build commands; no side effects")
    parser.add_argument("--build", action="store_true", help="also build selected event-service images; never flash")
    parser.add_argument("--out", type=Path, help="fresh image directory (default: ROOT/images)")
    parser.add_argument("--layout", choices=("one", "three"), default="three")
    args = parser.parse_args(argv)
    host = host_key(platform.system(), platform.machine())
    platforms = selected_platforms(args.platform)
    root = args.root.absolute()
    out = (args.out or root / "images").absolute()
    selected = assets(host, platforms)
    if args.plan:
        print(json.dumps({"host": host, "root": str(root), "platforms": platforms,
                          "downloads": selected, "zephyr_revision": zephyr.ZEPHYR_SHA,
                          "build_commands": [build_command(p, root, out / (("zephyr-c" if p == "zephyr" else p)
                                                          + "-" + args.layout), args.layout)
                                             for p in platforms]}, indent=2))
        return
    if sys.version_info < (3, 12):
        parser.error("Python 3.12+ is required for safe archive extraction")
    prerequisites = ["git", "cc", "bash"] + (["rustup", "cargo", "rustc"] if "embassy" in platforms else [])
    if "zephyr" in platforms:
        prerequisites += ["dtc"]
    missing = [name for name in prerequisites if not shutil.which(name)]
    if missing:
        parser.error("missing host tools: " + ", ".join(missing) + "; see README.md#optional-toolchain-setup")
    if args.build and (out.exists() or out.is_symlink()):
        parser.error("--build requires a fresh, non-symlink --out directory; existing images are preserved")
    root = prepare_root(root, host)
    with (root / ".setup.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another setup/build owns this root") from exc
        if "zephyr" in platforms:
            prepare_zephyr(root, selected)
        prepare_python(root, platforms)
        if "embassy" in platforms:
            prepare_embassy(root, selected)
        installed = tuple(json.loads((root / ".python-platforms.json").read_text()))
        write_environment(root, installed)
        if args.build:
            for name in platforms:
                case = ("zephyr-c" if name == "zephyr" else name) + "-" + args.layout
                try:
                    run(build_command(name, root, out / case, args.layout),
                        env=environment(root, name == "embassy"))
                except subprocess.CalledProcessError as exc:
                    raise ValueError(f"{case} build failed; logs and provenance are in {out / case}") from exc
    print(f"Setup ready: {root / 'environment.sh'}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit(f"Setup failed: {exc}")
    except KeyboardInterrupt:
        sys.exit("Setup interrupted; completed downloads are reusable on the next run.")
