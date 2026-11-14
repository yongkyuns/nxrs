//! Isolated, revision-pinned source archives and checked upstream adaptations.
use crate::{archive, hash, output, read_json, run, write_json, Result};
use serde_json::{json, Value};
use std::{
    fs,
    path::{Component, Path},
    process::{Command, Stdio},
};

pub fn git(source: &Path) -> Command {
    let mut command = Command::new("git");
    command
        .current_dir(source)
        .env("GIT_CEILING_DIRECTORIES", source.parent().unwrap_or(source));
    command
}
pub fn revision(root: &Path, name: &str) -> Result<String> {
    let revision = output(git(root).args(["rev-parse", &format!(":external/{name}")]))?;
    let revision = revision.trim();
    if revision.len() != 40 || !revision.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err("dependency requires a full Git revision".into());
    }
    Ok(revision.into())
}
pub fn archive_revision(source: &Path, revision: &str, destination: &Path) -> Result<()> {
    if destination.exists() {
        return Err(format!(
            "source archive destination already exists: {}",
            destination.display()
        ));
    }
    fs::create_dir(destination).map_err(|e| e.to_string())?;
    // Configuration inspection runs beside the terminal renderer. Keep Git's
    // diagnostic in the returned error instead of writing into the live screen.
    // A file keeps diagnostics from blocking the streamed archive.
    let diagnostics = crate::TempDir::new(&std::env::temp_dir())?;
    let stderr_path = diagnostics.0.join("git-archive.stderr");
    let stderr = fs::File::create(&stderr_path).map_err(|e| e.to_string())?;
    let mut child = git(source)
        .args(["archive", revision])
        .stdout(Stdio::piped())
        .stderr(stderr)
        .spawn()
        .map_err(|e| e.to_string())?;
    let result = archive::source_tree(
        child.stdout.take().unwrap(),
        destination,
        100000,
        1024 * 1024 * 1024,
    );
    if result.is_err() {
        let _ = child.kill();
    }
    let status = child.wait().map_err(|e| e.to_string())?;
    result?;
    if !status.success() {
        let stderr = fs::read(&stderr_path).map_err(|e| e.to_string())?;
        return Err(format!(
            "git archive failed for {}: {status}\n{}",
            source.display(),
            String::from_utf8_lossy(&stderr).trim()
        ));
    }
    Ok(())
}
fn safe_path(source: &Path, path: &str) -> Result<std::path::PathBuf> {
    let relative = Path::new(path);
    if relative
        .components()
        .any(|c| !matches!(c, Component::Normal(_)))
        || path.contains('\\')
    {
        return Err(format!("unsafe patch path: {path}"));
    }
    let p = source.join(relative);
    let mut ancestor = p.as_path();
    while !ancestor.exists() {
        ancestor = ancestor.parent().ok_or("patch path needs parent")?;
    }
    if !ancestor
        .canonicalize()
        .map_err(|e| e.to_string())?
        .starts_with(source)
    {
        return Err("patch path escapes isolated source".into());
    }
    Ok(p)
}
pub fn patch(source: &Path, patch: &Path) -> Result<Value> {
    let source = source.canonicalize().map_err(|e| e.to_string())?;
    let patch = patch.canonicalize().map_err(|e| e.to_string())?;
    // A file handle also works with Windows canonical paths that Git's argv parser rejects.
    let apply = |args: &[&str]| -> Result<String> {
        let input = fs::File::open(&patch).map_err(|e| e.to_string())?;
        output(
            git(&source)
                .args(["-c", "core.autocrlf=false", "apply"])
                .args(args)
                .stdin(input),
        )
    };
    let stats = apply(&["--numstat"])?;
    let paths = stats
        .lines()
        .map(|line| line.splitn(3, '\t').nth(2).ok_or("invalid patch numstat"))
        .collect::<std::result::Result<Vec<_>, _>>()?;
    if paths.is_empty() {
        return Err("patch has no source changes".into());
    }
    let mut files = serde_json::Map::new();
    for path in &paths {
        let p = safe_path(&source, path)?;
        files.insert(
            (*path).into(),
            json!({"before":if p.is_file(){Some(hash(&p)?)}else{None},"after":null}),
        );
    }
    apply(&["--check", "--whitespace=error"]).map_err(|error| {
        format!(
            "patch {} is incompatible or already applied: {error}",
            patch.display()
        )
    })?;
    apply(&["--whitespace=error"])?;
    for path in paths {
        let p = safe_path(&source, path)?;
        files[path]["after"] = json!(if p.is_file() { Some(hash(&p)?) } else { None });
    }
    Ok(
        json!({"name":patch.file_name().unwrap().to_string_lossy(),"sha256":hash(&patch)?,"files":files}),
    )
}
pub fn apply_series(
    root: &Path,
    source: &Path,
    component: &str,
    revision: &str,
    record: &Path,
    checkout: bool,
) -> Result<()> {
    if checkout && component != "esp-hal-3rdparty" {
        return Err("build checkout is only supported for isolated Espressif HAL copies".into());
    }
    let source = source.canonicalize().map_err(|e| e.to_string())?;
    if source.join(".git").exists() {
        if !checkout || component != "esp-hal-3rdparty" {
            return Err("patching requires an isolated source archive".into());
        }
        for (arg, expected) in [
            ("--show-toplevel", source.display().to_string()),
            (
                "--absolute-git-dir",
                source.join(".git").display().to_string(),
            ),
            ("HEAD", revision.into()),
        ] {
            if output(git(&source).args(["rev-parse", arg]))?.trim() != expected {
                return Err("independent HAL checkout identity/revision mismatch".into());
            }
        }
        run(git(&source).args(["diff", "--quiet", "--ignore-submodules=all", "HEAD"]))?;
    }
    let series = read_json(&root.join("upstream/build-patches.json"))?;
    let entry = &series[component];
    let marker = entry["marker"].as_str().ok_or("unknown source component")?;
    if !source.join(marker).is_file() {
        return Err(format!("not a {component} source archive"));
    }
    let mut patches = vec![];
    for name in entry["patches"].as_array().ok_or("invalid patch series")? {
        let name = name.as_str().ok_or("invalid patch name")?;
        if Path::new(name).components().count() != 1 {
            return Err("unsafe patch name".into());
        }
        patches.push(patch(
            &source,
            &root
                .join("upstream")
                .join(component)
                .join("patches")
                .join(name),
        )?);
    }
    write_json(
        record,
        &json!({"schema":1,"component":component,"upstream_revision":revision,"patches":patches}),
    )
}
pub fn release_version(root: &Path, revision: &str) -> Result<String> {
    let pin = read_json(&root.join("upstream/nuttx/release.json"))?;
    let version = pin["version"].as_str().ok_or("invalid release version")?;
    if pin["schema"] != 1
        || pin["revision"] != revision
        || !regex::Regex::new(r"^\d+\.\d+\.\d+$")
            .unwrap()
            .is_match(version)
    {
        return Err("selected NuttX revision does not match the release pin".into());
    }
    Ok(version.into())
}
pub fn stamp(root: &Path, source: &Path, revision: &str) -> Result<()> {
    if source.join(".git").exists() {
        return Err("release stamping requires a private source archive".into());
    }
    if revision.len() != 40
        || !revision
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err("version stamping requires a full source revision".into());
    }
    let version = release_version(root, revision)?;
    run(Command::new("bash")
        .arg(source.join("tools/version.sh"))
        .args(["-v", &version, "-b", &format!("{}-nxrs", &revision[..12])])
        .arg(source.join(".version")))
}
/// Copy SDK binaries and sources without hard-linking mutable files.
pub fn copy_tree(source: &Path, destination: &Path) -> Result<()> {
    if destination.exists() || destination.symlink_metadata().is_ok() {
        return Err(format!(
            "private destination must be new: {}",
            destination.display()
        ));
    }
    run(Command::new("cp")
        .arg(if std::env::consts::OS == "macos" {
            "-cR"
        } else {
            "-a"
        })
        .arg(source)
        .arg(destination))
}

pub use nxrs_build_support::source::verify_inventory;
pub fn prepare_inventory(
    ledger: &Path,
    patch_file: &Path,
    destination: &Path,
    checkout: Option<&Path>,
) -> Result<()> {
    if destination.exists() {
        return verify_inventory(ledger, destination);
    }
    let pin = read_json(ledger)?;
    let revision = pin["revision"].as_str().ok_or("missing SDK revision")?;
    fs::create_dir_all(destination.parent().ok_or("SDK destination parent")?)
        .map_err(|e| e.to_string())?;
    let temporary = crate::TempDir::new(destination.parent().unwrap())?;
    let repository = if let Some(path) = checkout {
        path.to_path_buf()
    } else {
        let path = temporary.0.join("git");
        run(Command::new("git")
            .args([
                "clone",
                "--depth",
                "1",
                "--branch",
                pin["tag"].as_str().ok_or("missing SDK tag")?,
                pin["repository"].as_str().ok_or("missing SDK repository")?,
            ])
            .arg(&path))?;
        path
    };
    if output(git(&repository).args(["rev-parse", "HEAD"]))?.trim() != revision {
        return Err("SDK revision differs from pinned source".into());
    }
    let staged = temporary.0.join("prepared");
    fs::create_dir(&staged).map_err(|e| e.to_string())?;
    for (name, expected) in pin["files"]
        .as_object()
        .ok_or("missing SDK source inventory")?
    {
        let path = safe_path(&staged, name)?;
        let bytes =
            crate::output_bytes(git(&repository).args(["show", &format!("{revision}:{name}")]))?;
        if crate::hash_bytes(&bytes) != expected.as_str().ok_or("invalid SDK source hash")? {
            return Err(format!("SDK source hash differs: {name}"));
        }
        fs::create_dir_all(path.parent().unwrap()).map_err(|e| e.to_string())?;
        fs::write(path, bytes).map_err(|e| e.to_string())?;
    }
    patch(&staged, patch_file)?;
    let identity = json!({"ledger_sha256":hash(ledger)?,"files":pin["prepared_files"]});
    write_json(&staged.join("prepared.json"), &identity)?;
    verify_inventory(ledger, &staged)?;
    fs::rename(staged, destination).map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_archive_preserves_contents_and_returns_git_diagnostics() {
        let temporary = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let source = temporary.0.join("source");
        fs::create_dir(&source).unwrap();
        output(git(&source).args(["init", "--quiet"])).unwrap();
        fs::write(source.join("input.txt"), "review fixture\n").unwrap();
        output(git(&source).args(["add", "input.txt"])).unwrap();
        output(git(&source).args([
            "-c",
            "user.name=NXRS test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ]))
        .unwrap();

        let archive = temporary.0.join("archive");
        archive_revision(&source, "HEAD", &archive).unwrap();
        assert_eq!(
            fs::read_to_string(archive.join("input.txt")).unwrap(),
            "review fixture\n"
        );
        let error = archive_revision(
            &source,
            "missing-review-revision",
            &temporary.0.join("missing"),
        )
        .unwrap_err();
        assert!(error.contains("git archive failed"), "{error}");
        assert!(error.contains("missing-review-revision"), "{error}");
    }
}
