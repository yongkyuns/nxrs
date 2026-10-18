"""Build optional native C and Objective-C sources for one HAL capability."""

from __future__ import annotations

import os
import platform
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


_SAFE_CRATE_SUFFIX = re.compile(r"^[A-Za-z0-9_]+$")
_SAFE_FRAMEWORK = re.compile(r"^[A-Za-z0-9_]+$")
_SAFE_PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")


def _native_metadata(manifest: Path, target_os: str) -> dict:
    try:
        with manifest.open("rb") as source:
            data = tomllib.load(source)
    except OSError as exc:
        raise RuntimeError(f"cannot read native build manifest {manifest}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"invalid native build manifest {manifest}: {exc}") from exc

    package = data.get("package", {})
    metadata = package.get("metadata", {}) if isinstance(package, dict) else {}
    nxrs = metadata.get("nxrs", {}) if isinstance(metadata, dict) else {}
    native_targets = nxrs.get("native", {}) if isinstance(nxrs, dict) else {}
    native = native_targets.get(target_os, {}) if isinstance(native_targets, dict) else {}
    if not isinstance(native, dict):
        raise RuntimeError(f"{manifest}: package.metadata.nxrs.native.{target_os} must be a table")
    return native


def _frameworks(manifest: Path, native: dict, target_os: str) -> list[str]:
    frameworks = native.get("frameworks", [])
    if not isinstance(frameworks, list) or any(
        not isinstance(name, str) or not _SAFE_FRAMEWORK.fullmatch(name)
        for name in frameworks
    ):
        raise RuntimeError(
            f"{manifest}: package.metadata.nxrs.native.{target_os}.frameworks "
            "must be an array of simple framework names"
        )
    return sorted(set(frameworks))


def _pkg_config(manifest: Path, native: dict, target_os: str) -> list[str]:
    packages = native.get("pkg-config", [])
    if not isinstance(packages, list) or any(
        not isinstance(name, str) or not _SAFE_PACKAGE.fullmatch(name)
        for name in packages
    ):
        raise RuntimeError(
            f"{manifest}: package.metadata.nxrs.native.{target_os}.pkg-config "
            "must be an array of simple package names"
        )
    return sorted(set(packages))


def _pkg_flags(output: str, kind: str) -> tuple[list[str], list[str]]:
    """Split pkg-config flags into compiler arguments and Cargo link metadata."""
    try:
        words = shlex.split(output)
    except ValueError as exc:
        raise RuntimeError(f"malformed pkg-config {kind} output: {exc}") from exc
    compile_flags: list[str] = []
    link_directives: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        if kind == "cflags":
            if word in {"-I", "-D", "-U", "-isystem", "-include"}:
                index += 1
                if index == len(words) or not words[index]:
                    raise RuntimeError(f"malformed pkg-config {kind} output: missing value after {word}")
                compile_flags.extend((word, words[index]))
            elif word.startswith(("-I", "-D", "-U")) and len(word) > 2:
                compile_flags.append(word)
            elif word == "-pthread":
                compile_flags.append(word)
            elif word.startswith("-f") and len(word) > 2:
                compile_flags.append(word)
            else:
                raise RuntimeError(f"unrecognized pkg-config {kind} flag: {word}")
        else:
            if word in {"-L", "-l"}:
                index += 1
                if index == len(words) or not words[index]:
                    raise RuntimeError(f"malformed pkg-config {kind} output: missing value after {word}")
                value = words[index]
                link_directives.append(
                    f"cargo:rustc-link-search=native={value}" if word == "-L"
                    else f"cargo:rustc-link-lib={value}"
                )
            elif word.startswith("-L") and len(word) > 2:
                link_directives.append(f"cargo:rustc-link-search=native={word[2:]}")
            elif word.startswith("-l") and len(word) > 2:
                link_directives.append(f"cargo:rustc-link-lib={word[2:]}")
            elif word == "-pthread":
                # rustc-link-arg does not propagate from this library's build
                # script to consumers. A library dependency does.
                link_directives.append("cargo:rustc-link-lib=pthread")
            else:
                raise RuntimeError(f"unrecognized pkg-config {kind} flag: {word}")
        index += 1
    return compile_flags, link_directives


def _env_command(name: str, default: str) -> list[str]:
    value = os.environ.get(name, default)
    try:
        command = shlex.split(value)
    except ValueError as exc:
        raise RuntimeError(f"invalid {name} command: {exc}") from exc
    if not command:
        raise RuntimeError(f"{name} must name a command")
    return command


def _host_arch() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)


def _target_arch(target_triple: str) -> str:
    machine = target_triple.split("-", 1)[0].lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)


def _run(command: list[str], description: str, source: Path | None = None) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        where = f" for {source}" if source is not None else ""
        message = f"native {description} failed{where} (exit {exc.returncode})"
        if detail:
            message += f": {detail}"
        raise RuntimeError(message) from exc
    except OSError as exc:
        raise RuntimeError(f"cannot run native {description}: {exc}") from exc


def build(
    capability_dir: Path, target_os: str, target_triple: str, out_dir: Path
) -> list[str]:
    """Compile a capability's optional native sources and return Cargo directives.

    Native sources live in ``ffi/<target_os>``. Only native-host macOS and Linux
    builds are supported; cross compilation is deliberately rejected.
    """
    capability_dir = Path(capability_dir)
    out_dir = Path(out_dir)
    ffi_dir = capability_dir / "ffi"
    source_dir = ffi_dir / target_os
    # Also watch empty/missing source folders via their nearest existing
    # ancestor, so adding the first source (or re-adding the last) reruns Cargo.
    watched = {capability_dir / 'Cargo.toml'}
    for directory in (source_dir, ffi_dir / 'native'):
        while not directory.is_dir() and directory != capability_dir:
            directory = directory.parent
        watched.add(directory)
    inputs = [f"cargo:rerun-if-changed={path}" for path in sorted(watched)]
    sources = sorted(
        (path for path in source_dir.glob("*") if path.is_file() and path.suffix in {".c", ".m"}),
        key=lambda path: path.name,
    ) if source_dir.is_dir() else []
    if not sources:
        return inputs
    inputs.extend(f"cargo:rerun-if-changed={path}" for path in sources)

    crate_suffix = capability_dir.name
    if not _SAFE_CRATE_SUFFIX.fullmatch(crate_suffix):
        raise RuntimeError(f"unsafe capability directory name for native archive: {crate_suffix!r}")
    if target_os not in {"macos", "linux"}:
        raise RuntimeError(f"native sources for target OS {target_os!r} are not supported yet")

    manifest = capability_dir / "Cargo.toml"
    native = _native_metadata(manifest, target_os)
    frameworks = _frameworks(manifest, native, target_os) if target_os == "macos" else []
    packages = _pkg_config(manifest, native, target_os) if target_os == "linux" else []
    if target_os == "macos":
        if sys.platform != "darwin" and not platform.mac_ver()[0]:
            raise RuntimeError("native macOS sources can only be compiled on a macOS host")
    else:
        if sys.platform != "linux":
            raise RuntimeError("native Linux sources can only be compiled on a Linux host")
        if _target_arch(target_triple) != _host_arch():
            raise RuntimeError("native Linux sources require a target architecture matching the host")
        if not target_triple.endswith("-linux-gnu"):
            raise RuntimeError("native Linux system libraries currently require a GNU target")
        if any(source.suffix != ".c" for source in sources):
            raise RuntimeError("native Linux bridges must use C sources")

    shared_dir = ffi_dir / "native"
    headers = sorted(
        path for directory in (source_dir, shared_dir) if directory.is_dir()
        for path in directory.rglob("*") if path.is_file() and path.suffix in {".h", ".hpp"}
    )
    for directory in (source_dir, shared_dir):
        if directory.is_dir():
            inputs.extend(
                f"cargo:rerun-if-changed={path}"
                for path in directory.rglob("*") if path.is_dir()
            )
    inputs.extend(f"cargo:rerun-if-changed={path}" for path in headers)
    out_dir.mkdir(parents=True, exist_ok=True)
    objects: list[Path] = []
    compile_flags: list[str] = []
    link_directives: list[str] = []
    if target_os == "linux":
        pkg_config = _env_command("PKG_CONFIG", "pkg-config")
        env_inputs = [
            "PKG_CONFIG", "PKG_CONFIG_PATH", "PKG_CONFIG_LIBDIR",
            "PKG_CONFIG_SYSROOT_DIR", "CC", "AR",
        ]
        inputs.extend(f"cargo:rerun-if-env-changed={name}" for name in env_inputs)
        if packages:
            for kind in ("cflags", "libs"):
                try:
                    result = subprocess.run(
                        [*pkg_config, f"--{kind}", *packages],
                        check=True, capture_output=True, text=True,
                    )
                except subprocess.CalledProcessError as exc:
                    detail = (exc.stderr or exc.stdout or "").strip()
                    message = f"native pkg-config {kind} failed (exit {exc.returncode})"
                    if detail:
                        message += f": {detail}"
                    raise RuntimeError(message) from exc
                except OSError as exc:
                    raise RuntimeError(f"cannot run native pkg-config: {exc}") from exc
                flags, directives = _pkg_flags(result.stdout, kind)
                if kind == "cflags":
                    compile_flags.extend(flags)
                else:
                    link_directives.extend(directives)
        compiler = _env_command("CC", "cc")
        archiver = _env_command("AR", "ar")
    for index, source in enumerate(sources):
        object_path = out_dir / f"nxrs_{crate_suffix}_{index:04d}.o"
        if target_os == "macos":
            command = [
                "xcrun", "--sdk", "macosx", "clang", "-target", target_triple,
                "-Wall", "-Wextra", "-Werror",
            ]
            if source.suffix == ".m":
                command.append("-fobjc-arc")
        else:
            command = [*compiler, "-Wall", "-Wextra", "-Werror", *compile_flags]
        command.extend([
            "-I", str(source_dir), "-I", str(shared_dir),
            "-c", str(source), "-o", str(object_path),
        ])
        _run(command, "compilation", source)
        objects.append(object_path)

    archive = out_dir / f"libnxrs_{crate_suffix}_native.a"
    # ar updates existing archives and would retain members from removed sources.
    # Build a fresh archive and replace only this owned output after success.
    with tempfile.TemporaryDirectory(prefix="nxrs-native-", dir=out_dir) as temporary:
        fresh = Path(temporary) / archive.name
        archive_command = (
            ["ar", "crs", str(fresh), *(str(obj) for obj in objects)]
            if target_os == "macos"
            else [*archiver, "crs", str(fresh), *(str(obj) for obj in objects)]
        )
        _run(archive_command, "archive creation")
        if not fresh.is_file():
            raise RuntimeError("native archive command produced no archive")
        fresh.replace(archive)

    environment_inputs = (
        ("DEVELOPER_DIR", "SDKROOT", "MACOSX_DEPLOYMENT_TARGET")
        if target_os == "macos" else ()
    )
    directives = [
        f"cargo:rustc-link-search=native={out_dir}",
        f"cargo:rustc-link-lib=static=nxrs_{crate_suffix}_native",
        *link_directives,
        *(f"cargo:rustc-link-lib=framework={name}" for name in frameworks),
        *(f"cargo:rerun-if-env-changed={name}" for name in environment_inputs),
        *dict.fromkeys(inputs),
    ]
    return directives
