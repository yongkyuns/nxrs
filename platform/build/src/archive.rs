//! Bounded extraction into an owned build directory; no archive-supplied paths escape it.
use crate::Result;
use std::{
    fs,
    io::Read,
    path::{Component, Path, PathBuf},
    process::{Command, Stdio},
};
pub fn extract(reader: impl Read, out: &Path, max_files: usize, max_bytes: u64) -> Result<()> {
    extract_scoped(reader, out, max_files, max_bytes, None, Path::new(""))
}
fn extract_root(
    reader: impl Read,
    out: &Path,
    max_files: usize,
    max_bytes: u64,
    root: Option<&Path>,
) -> Result<()> {
    extract_scoped(reader, out, max_files, max_bytes, root, Path::new(""))
}
pub fn source_tree(reader: impl Read, out: &Path, max_files: usize, max_bytes: u64) -> Result<()> {
    // NuttX deliberately links fs/zipfs/zlib to the sibling apps archive.
    // Both are isolated under the same owned build directory.
    let prefix = Path::new(out.file_name().ok_or("source tree needs a name")?);
    extract_scoped(reader, out, max_files, max_bytes, None, prefix)
}
fn extract_scoped(
    reader: impl Read,
    out: &Path,
    max_files: usize,
    max_bytes: u64,
    root: Option<&Path>,
    link_prefix: &Path,
) -> Result<()> {
    fs::create_dir_all(out).map_err(|e| e.to_string())?;
    let mut archive = tar::Archive::new(reader);
    archive.set_preserve_permissions(false);
    let mut bytes = 0u64;
    for (index, entry) in archive.entries().map_err(|e| e.to_string())?.enumerate() {
        let mut entry = entry.map_err(|e| e.to_string())?;
        let kind = entry.header().entry_type();
        bytes = bytes
            .checked_add(entry.header().size().map_err(|e| e.to_string())?)
            .ok_or("archive size overflow")?;
        if index >= max_files || bytes > max_bytes {
            return Err("release exceeds source bound".into());
        }
        if kind.is_pax_global_extensions() {
            continue;
        }
        let path = entry.path().map_err(|e| e.to_string())?.into_owned();
        if root.is_some_and(|root| !path.starts_with(root)) {
            return Err("unexpected archive root".into());
        }
        if kind.is_symlink() || kind.is_hard_link() {
            let path = entry.path().map_err(|e| e.to_string())?;
            let target = entry
                .link_name()
                .map_err(|e| e.to_string())?
                .ok_or("missing archive link target")?;
            let target = if kind.is_symlink() {
                path.parent().unwrap_or(Path::new("")).join(target)
            } else {
                target.into_owned()
            };
            let target = contained(&link_prefix.join(&target))?;
            if root.is_some_and(|root| !target.starts_with(root)) {
                return Err("archive link escapes release root".into());
            }
        } else if !kind.is_file() && !kind.is_dir() {
            return Err("release contains special files".into());
        }
        if !entry.unpack_in(out).map_err(|e| e.to_string())? {
            return Err("release path escapes build directory".into());
        }
    }
    Ok(())
}
fn contained(path: &Path) -> Result<PathBuf> {
    let mut result = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Normal(part) => result.push(part),
            Component::CurDir => {}
            Component::ParentDir if result.pop() => {}
            _ => return Err("archive link escapes build directory".into()),
        }
    }
    Ok(result)
}
pub fn bzip2(source: &Path, destination: &Path) -> Result<()> {
    compressed("bzip2", source, destination, 10000, 128 * 1024 * 1024)
}
pub fn compressed(
    program: &str,
    source: &Path,
    destination: &Path,
    max_files: usize,
    max_bytes: u64,
) -> Result<()> {
    let parent = destination
        .parent()
        .ok_or("archive destination needs a parent")?;
    let staging = crate::TempDir::new(parent)?;
    let out = &staging.0;
    let name = destination
        .file_name()
        .ok_or("archive destination needs a name")?;
    let mut child = Command::new(program)
        .arg("-dc")
        .arg(source)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| e.to_string())?;
    let result = extract_root(
        child.stdout.take().unwrap(),
        out,
        max_files,
        max_bytes,
        Some(Path::new(name)),
    );
    if result.is_err() {
        let _ = child.kill();
    }
    let output = child.wait_with_output().map_err(|e| e.to_string())?;
    result?;
    if !output.status.success() {
        return Err(format!(
            "{program}: {}",
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    let staged = out.join(name);
    if fs::read_dir(out).map_err(|e| e.to_string())?.count() != 1
        || !staged
            .symlink_metadata()
            .map_err(|e| e.to_string())?
            .is_dir()
    {
        return Err("release top-level directory differs from pinned version".into());
    }
    fs::rename(staged, destination).map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn release(link: &str) -> Vec<u8> {
        let mut tar = tar::Builder::new(Vec::new());
        let mut h = tar::Header::new_gnu();
        h.set_entry_type(tar::EntryType::Directory);
        h.set_size(0);
        h.set_mode(0o755);
        h.set_cksum();
        tar.append_data(&mut h, "release", std::io::empty())
            .unwrap();
        let mut h = tar::Header::new_gnu();
        h.set_entry_type(tar::EntryType::Symlink);
        h.set_size(0);
        h.set_mode(0o777);
        h.set_link_name(link).unwrap();
        h.set_cksum();
        tar.append_data(&mut h, "release/link", std::io::empty())
            .unwrap();
        tar.into_inner().unwrap()
    }
    #[test]
    fn published_links_remain_within_release() {
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        for target in ["..", "../../outside", "/outside"] {
            let out = temp.0.join("bad");
            assert!(extract_root(
                release(target).as_slice(),
                &out,
                10,
                1024,
                Some(Path::new("release"))
            )
            .is_err());
            crate::remove_dir(&out).unwrap();
        }
        #[cfg(unix)]
        {
            let out = temp.0.join("valid");
            extract_root(
                release(".").as_slice(),
                &out,
                10,
                1024,
                Some(Path::new("release")),
            )
            .unwrap();
            assert_eq!(
                out.join("release/link").canonicalize().unwrap(),
                out.join("release").canonicalize().unwrap()
            );
        }
        assert!(extract_root(
            release(".").as_slice(),
            &temp.0.join("bound"),
            1,
            1024,
            Some(Path::new("release"))
        )
        .is_err());
    }
}
