//! Linux policy and ownership remain Rust. No root, user namespace, service
//! mutation, pre-exec callback or best-effort downgrade is required.
use landlock::{
    Access, AccessFs, CompatLevel, Compatible, PathBeneath, PathFd, Ruleset, RulesetAttr,
    RulesetCreatedAttr, RulesetStatus, ABI,
};
use std::{
    ffi::OsString,
    io,
    os::unix::{fs::MetadataExt, process::CommandExt},
    path::Path,
    process::Command,
};
#[allow(unsafe_code)]
mod syscall;
fn refused(_: impl std::fmt::Debug) -> io::Error {
    io::ErrorKind::PermissionDenied.into()
}
fn restrict(program: Option<&Path>, work: Option<&Path>) -> io::Result<()> {
    // V3 includes cross-directory refer and truncate restrictions. Hard
    // requirements prohibit silently dropping any of the requested rights.
    syscall::close_inherited()?;
    let abi = ABI::V3;
    let mut rules = Ruleset::default()
        .set_compatibility(CompatLevel::HardRequirement)
        .handle_access(AccessFs::from_all(abi))
        .map_err(refused)?
        .create()
        .map_err(refused)?;
    let read = AccessFs::ReadFile | AccessFs::ReadDir | AccessFs::Execute;
    for path in nxrs_media_sandbox::LIBRARIES {
        if Path::new(path).exists() {
            rules = rules
                .add_rule(PathBeneath::new(PathFd::new(path).map_err(refused)?, read))
                .map_err(refused)?;
        }
    }
    for path in nxrs_media_sandbox::READ_FILES {
        if Path::new(path).exists() {
            rules = rules
                .add_rule(PathBeneath::new(
                    PathFd::new(path).map_err(refused)?,
                    AccessFs::ReadFile,
                ))
                .map_err(refused)?;
        }
    }
    // Only the selected executable, not all of /usr/bin or the launcher's
    // directory, receives read/execute rights.
    if let Some(path) = program {
        if !path.is_absolute() || !std::fs::metadata(path)?.is_file() {
            return Err(refused(()));
        }
        rules = rules
            .add_rule(PathBeneath::new(
                PathFd::new(path).map_err(refused)?,
                AccessFs::ReadFile | AccessFs::Execute,
            ))
            .map_err(refused)?;
    }
    let null = PathFd::new("/dev/null").map_err(refused)?;
    rules = rules
        .add_rule(PathBeneath::new(
            null,
            AccessFs::ReadFile | AccessFs::WriteFile,
        ))
        .map_err(refused)?;
    if let Some(path) = work {
        let meta = std::fs::symlink_metadata(path)?;
        let real = std::fs::canonicalize(path)?;
        if !path.is_absolute()
            || path != real
            || !meta.is_dir()
            || meta.uid() != syscall::uid()
            || meta.mode() & 0o077 != 0
            || path.parent().is_none()
            || nxrs_media_sandbox::LIBRARIES
                .iter()
                .any(|library| Path::new(library).starts_with(path) || path.starts_with(library))
        {
            return Err(refused(()));
        }
        rules = rules
            .add_rule(PathBeneath::new(
                PathFd::new(path).map_err(refused)?,
                AccessFs::from_all(abi) & !AccessFs::Execute,
            ))
            .map_err(refused)?;
    }
    let status = rules.restrict_self().map_err(refused)?;
    if status.ruleset != RulesetStatus::FullyEnforced || !status.no_new_privs {
        return Err(refused(()));
    }
    // Landlock's domain hierarchy also protects the unsandboxed parent from
    // ptrace-related access. Seccomp separately denies sockets, io_uring,
    // process inspection/FD stealing and signal injection.
    syscall::restrict()?;
    Ok(())
}
pub fn check() -> io::Result<()> {
    restrict(None, None)
}
pub fn run(program: &Path, arguments: &[OsString], work: Option<&Path>) -> io::Result<()> {
    let program = std::fs::canonicalize(program)?;
    restrict(Some(&program), work)?;
    Err(Command::new(program).args(arguments).exec())
}
