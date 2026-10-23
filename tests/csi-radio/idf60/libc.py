"""Prepare a private Rust sysroot source with SDK-qualified ESP-IDF alignments.

Only the copied target bindings change. Never modify the camera/NuttX sysroot.
"""

from pathlib import Path
import shutil
import shlex


def fix_alignment(source):
    for name in ("pthread_mutex_t", "pthread_rwlock_t", "pthread_cond_t"):
        marker = "    pub struct " + name + " {"
        end = source.index(marker)
        start = source.rfind("\n    #[", 0, end)
        # Mutex and rwlock have two cfg_attr blocks. Include both attributes.
        if name != "pthread_cond_t":
            start = source.rfind("\n    #[", 0, start)
        attrs = source[start:end]
        if "repr(align(8))" not in attrs:
            raise ValueError("unrecognized libc synchronization alignment")
        attrs = attrs.replace(
            "#[cfg_attr(\n        all(",
            '#[cfg_attr(\n        any(target_os = "espidf", all(',
            1,
        )
        if name != "pthread_cond_t":
            attrs = attrs.replace(
                "        ),\n        repr(align(4))",
                "        )),\n        repr(align(4))",
                1,
            )
            attrs = attrs.replace(
                "#[cfg_attr(\n        any(\n",
                '#[cfg_attr(\n        all(not(target_os = "espidf"), any(\n',
                1,
            )
            attrs = attrs.replace(
                "        ),\n        repr(align(8))",
                "        )),\n        repr(align(8))",
                1,
            )
        else:
            attrs = attrs.replace(
                "#[repr(align(8))]",
                '#[cfg_attr(target_os = "espidf", repr(align(4)))]\n    #[cfg_attr(not(target_os = "espidf"), repr(align(8)))]',
            )
        source = source[:start] + attrs + source[end:]
    return source


def prepare(sysroot, destination):
    sysroot, destination = Path(sysroot), Path(destination)
    library = destination / "lib/rustlib/src/rust/library"
    if not library.exists():
        library.parent.mkdir(parents=True)
        shutil.copytree(sysroot / "lib/rustlib/src/rust/library", library)
        for item in (sysroot / "lib/rustlib").iterdir():
            if item.name != "src":
                (destination / "lib/rustlib" / item.name).symlink_to(
                    item, target_is_directory=item.is_dir()
                )
    libc = library / "libc-idf60"
    if not libc.exists():
        shutil.copytree(library / "libc-nuttx", libc)
    original = (library / "libc-nuttx/src/unix/newlib/mod.rs").read_text()
    (libc / "src/unix/newlib/mod.rs").write_text(fix_alignment(original))
    esp = libc / "src/unix/newlib/espidf/mod.rs"
    text = esp.read_text()
    if "pub const POLLNVAL:" not in text:
        text += "\n// IDF 6.0 sys/poll.h; covered by the compiled SDK witness.\npub const POLLNVAL: crate::c_short = 1 << 7;\n"
    esp.write_text(text)
    manifest = library / "Cargo.toml"
    source = manifest.read_text()
    old, new = "libc = { path = 'libc-nuttx' }", "libc = { path = 'libc-idf60' }"
    if old not in source and new not in source:
        raise ValueError("unrecognized Rust standard library libc patch")
    manifest.write_text(source.replace(old, new))
    compiler = destination / "rustc"
    compiler.write_text(
        "#!/bin/sh\nexec "
        + shlex.quote(str(sysroot / "bin/rustc"))
        + " --sysroot "
        + shlex.quote(str(destination))
        + ' "$@"\n'
    )
    compiler.chmod(0o755)
    return compiler, libc
