//! Public process-launch protocol shared intentionally by ingest and recording.
#![forbid(unsafe_code)]
use std::{
    io,
    path::{Component, Path},
    process::Command,
};

/// No shell or credential-bearing argument. Work grants refer only to a private
/// per-attempt recording/finalization directory, never the activity root.
pub fn command(launcher: &Path, program: &Path, work: Option<&Path>) -> Command {
    let mut command = Command::new(launcher);
    command
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("LANG", "C");
    if let Some(directory) = work {
        command.arg("--work").arg(directory);
    }
    command.arg("--").arg(program);
    command
}

pub const LIBRARIES: &[&str] = &["/lib", "/lib64", "/usr/lib", "/usr/lib64"];
pub const READ_FILES: &[&str] = &["/etc/ld.so.cache", "/etc/localtime"];

/// Reject credential placement inside the decoder's read grants. Check both
/// lexical and resolved names, including aliases into an allowed hierarchy.
/// Files must already exist; this cannot silently accept a missing credential.
pub fn check_credentials(program: &Path, paths: &[&Path], work: Option<&Path>) -> io::Result<()> {
    let executable = std::fs::canonicalize(program)?;
    let work = work.map(std::fs::canonicalize).transpose()?;
    let readable_files: Vec<_> = READ_FILES
        .iter()
        .filter_map(|path| std::fs::canonicalize(path).ok())
        .collect();
    for path in paths {
        if !path.is_absolute() || path.components().any(|p| matches!(p, Component::ParentDir)) {
            return Err(io::ErrorKind::InvalidInput.into());
        }
        let resolved = std::fs::canonicalize(path)?;
        for name in [*path, resolved.as_path()] {
            if name == executable
                || LIBRARIES.iter().any(|root| name.starts_with(root))
                || READ_FILES.iter().any(|root| name == Path::new(root))
                || readable_files.iter().any(|root| name == root)
                || work.as_ref().is_some_and(|root| name.starts_with(root))
            {
                return Err(io::ErrorKind::PermissionDenied.into());
            }
        }
    }
    Ok(())
}
