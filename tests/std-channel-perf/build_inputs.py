"""Source closure of the Rust build frontend used by the integrated fixtures.

This is input selection, not another build system. Historical revisions without
this helper retain their original replay policy. Never infer inputs from a
receipt: select regular source files from the working tree or recorded Git tree.
"""
from pathlib import Path

MARKER = "tests/std-channel-perf/build_inputs.py"
FIXED = (
    MARKER, ".cargo/config.toml", "rust-toolchain.toml",
    "upstream/build-patches.json", "platform/build/Cargo.toml",
    "hal/detector/build_esp_dl.rs",
)


def select(names):
    names = set(names)
    selected = set(FIXED)
    selected.update(name for name in names
                    if name.startswith("platform/build/")
                    and Path(name).suffix in (".rs", ".toml")
                    and not {"target", "__pycache__"}.intersection(Path(name).parts))
    missing = selected - names
    if missing or "platform/build/src/main.rs" not in selected or "platform/build/src/lib.rs" not in selected:
        raise ValueError("incomplete Rust build frontend inputs: " + ", ".join(sorted(missing)))
    return sorted(selected)


def files(root):
    root = Path(root).resolve(strict=True)
    names = set(FIXED)
    names.update(path.relative_to(root).as_posix()
                 for path in (root / "platform/build").rglob("*") if path.is_file())
    result = []
    for name in select(names):
        path = root / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError("nonregular Rust build frontend input: " + name)
        result.append(path)
    return result
