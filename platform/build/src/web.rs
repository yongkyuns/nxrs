//! Browser std-thread builds using the pinned Emscripten SDK and a private Rust SDK.
use crate::{
    build::{self, App, Options},
    deployment, hash, output,
    profile::Profile,
    read, reuse, source, write_json, Result,
};
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};
const TOOLCHAIN: &str = "nightly-2026-09-25";
const TARGET: &str = "wasm32-unknown-emscripten";
const TLS: &str = "lib/rustlib/src/rust/library/std/src/sys/thread_local/mod.rs";
const RUSTFLAGS: &str = "-C target-feature=+atomics,+bulk-memory,+mutable-globals -C panic=abort -C link-arg=-pthread -C link-arg=-sPROXY_TO_PTHREAD=1 -C link-arg=-sPTHREAD_POOL_SIZE=4 -C link-arg=-sPTHREAD_POOL_SIZE_STRICT=2 -C link-arg=-sALLOW_BLOCKING_ON_MAIN_THREAD=0 -C link-arg=-sEXIT_RUNTIME=1 -C link-arg=-sINITIAL_MEMORY=67108864 -C link-arg=-sALLOW_MEMORY_GROWTH=0 -C link-arg=-sSTACK_SIZE=2097152 -C link-arg=-sDEFAULT_PTHREAD_STACK_SIZE=2097152 -C link-arg=-sASSERTIONS=2 -C link-arg=-sENVIRONMENT=web,worker";
const EMCC_CFLAGS: &str = "-pthread";
const STD_INPUTS: &str = "std-inputs";

fn transform_tls(text: &str) -> Result<String> {
    let guard = "            all(target_family = \"wasm\", not(target_env = \"p3\")),\n";
    let key = "            all(not(target_vendor = \"apple\"), not(target_family = \"wasm\"), target_family = \"unix\"),\n";
    if text.matches(guard).count() != 1 || text.matches(key).count() != 1 {
        return Err("unexpected browser TLS selection layout".into());
    }
    let changed=text.replace(guard,"            all(\n                target_family = \"wasm\",\n                not(target_env = \"p3\"),\n                not(all(target_os = \"emscripten\", target_feature = \"atomics\")),\n            ),\n").replace(key,&format!("{key}            all(target_os = \"emscripten\", target_feature = \"atomics\"),\n"));
    Ok(changed)
}
fn tls_fix(source: &Path, dest: &Path, receipts: &Path) -> Result<()> {
    let original = source.join(TLS);
    let before = output(Command::new("git").arg("hash-object").arg(&original))?;
    if before.trim() != "809ef5cfe9c60dd7ca99d587a9246751c1be338f" {
        return Err("unexpected browser SDK source; refuse drift/reapplication".into());
    }
    let text = read(&original)?;
    let changed = transform_tls(&text)?;
    source::copy_tree(source, dest)?;
    let path = dest.join(TLS);
    if !path
        .canonicalize()
        .map_err(|e| e.to_string())?
        .starts_with(dest.canonicalize().map_err(|e| e.to_string())?)
    {
        return Err("browser SDK source escaped private copy".into());
    }
    reuse::write_if_changed(&path, changed.as_bytes())?;
    if read(&original)? != text {
        return Err("installed browser SDK changed".into());
    }
    let diff = Command::new("diff")
        .args([
            "-u",
            "--label",
            &format!("a/{TLS}"),
            "--label",
            &format!("b/{TLS}"),
        ])
        .arg(original)
        .arg(&path)
        .output()
        .map_err(|e| e.to_string())?;
    if diff.status.code() != Some(1) {
        return Err("expected explicit TLS source change".into());
    }
    reuse::write_if_changed(&receipts.join("std-tls.patch"), &diff.stdout)?;
    write_json(
        &receipts.join("std-patch.json"),
        &json!({"mode":"emscripten-atomic-tls-key-cleanup","source_path":TLS,"original_blob":before.trim(),"patched_blob":output(Command::new("git").arg("hash-object").arg(path))?.trim(),"installed_toolchain_unchanged":true}),
    )
}
pub fn prepare_std(source: &Path, out: &Path) -> Result<()> {
    let receipts = out.join(STD_INPUTS);
    fs::create_dir_all(&receipts).map_err(|e| e.to_string())?;
    tls_fix(source, &out.join("toolchain"), &receipts)?;
    let site = out.join("site");
    fs::create_dir_all(&site).map_err(|e| e.to_string())?;
    copy_std_receipts(&receipts, &site, true)
}

fn cache_directories(fix: bool) -> Vec<&'static str> {
    let mut directories = vec![STD_INPUTS];
    if fix {
        directories.push("toolchain");
    }
    directories
}

fn sdk_identity(installed: &Path, emcc: &str, emcc_program: &Path) -> Result<Value> {
    // The rustc launcher alone does not identify its shared compiler libraries or
    // rust-src. Same-path changes to any of those bytes invalidate the copy.
    Ok(json!({
        "installed": installed,
        "rust_sdk": reuse::snapshot(installed, &["bin", "lib"])?,
        "emscripten": emcc.trim(),
        "emcc_program": emcc_program,
        "emcc_sha256": hash(emcc_program)?,
    }))
}

fn clear_evidence(out: &Path) -> Result<()> {
    crate::remove_dir(&out.join("site"))?;
    for name in ["provider-selection.json", "cargo-messages.jsonl"] {
        crate::remove_file(&out.join(name))?;
    }
    Ok(())
}

fn begin_build(out: &Path, inputs: &Value, fix: bool, clean: bool) -> Result<bool> {
    let reused = reuse::begin(out, inputs, &cache_directories(fix), clean)?;
    if !reused {
        crate::remove_dir(&out.join("cargo"))?;
        // Also remove an unused private SDK when switching to unmodified std.
        crate::remove_dir(&out.join("toolchain"))?;
    }
    clear_evidence(out)?;
    fs::create_dir(out.join("site")).map_err(|e| e.to_string())?;
    fs::create_dir_all(out.join(STD_INPUTS)).map_err(|e| e.to_string())?;
    Ok(reused)
}

fn copy_std_receipts(receipts: &Path, site: &Path, fix: bool) -> Result<()> {
    for name in if fix {
        &["std-patch.json", "std-tls.patch"][..]
    } else {
        &["std-patch.json"][..]
    } {
        reuse::copy_if_changed(&receipts.join(name), &site.join(name))?;
    }
    Ok(())
}

/// Select the requested package and binary from this invocation's Cargo output.
/// The manifest path identifies the package without parsing opaque Package IDs.
fn selected_artifact(messages: &str, app: &App) -> Result<Value> {
    let manifest = app.manifest.canonicalize().map_err(|e| e.to_string())?;
    let mut selected = Vec::new();
    for line in messages.lines() {
        let message: Value =
            serde_json::from_str(line).map_err(|e| format!("invalid Cargo message: {e}"))?;
        if message["reason"] == "compiler-artifact"
            && message["target"]["name"] == app.bin
            && message["target"]["kind"]
                .as_array()
                .is_some_and(|kinds| kinds.iter().any(|kind| kind == "bin"))
            && message["profile"]["test"] != true
            && message["manifest_path"].as_str().is_some_and(|path| {
                Path::new(path)
                    .canonicalize()
                    .is_ok_and(|path| path == manifest)
            })
        {
            selected.push(message);
        }
    }
    if selected.len() != 1 {
        return Err(format!(
            "expected one Cargo artifact for {}/{}, found {}",
            app.package,
            app.bin,
            selected.len()
        ));
    }
    Ok(selected.pop().unwrap())
}

#[derive(Debug)]
struct BrowserArtifacts {
    js: PathBuf,
    files: BTreeSet<PathBuf>,
}

fn browser_artifacts(artifact: &Value, cargo_dir: &Path) -> Result<BrowserArtifacts> {
    let root = cargo_dir.canonicalize().map_err(|e| e.to_string())?;
    let filenames = artifact["filenames"]
        .as_array()
        .ok_or("Cargo artifact is missing filenames")?
        .iter()
        .map(|path| {
            path.as_str()
                .map(PathBuf::from)
                .ok_or("invalid Cargo artifact filename".into())
        })
        .collect::<Result<Vec<_>>>()?;
    let js = PathBuf::from(
        artifact["executable"]
            .as_str()
            .ok_or("Cargo browser artifact is missing its executable")?,
    );
    if js.extension().is_none_or(|extension| extension != "js") || !filenames.contains(&js) {
        return Err("Cargo browser executable must be a reported JavaScript artifact".into());
    }
    let wasm: Vec<_> = filenames
        .iter()
        .filter(|path| {
            path.extension()
                .is_some_and(|extension| extension == "wasm")
        })
        .collect();
    if wasm.len() != 1 {
        return Err(format!(
            "expected one reported WASM artifact, found {}",
            wasm.len()
        ));
    }
    // Cargo can publish `foo-bar.js` beside `foo_bar.wasm`; never guess the WASM
    // filename from the JS name, or scan cached output from other applications.
    // The pinned Cargo reports that auxiliary WASM output in `filenames`:
    // https://github.com/rust-lang/cargo/blob/98a09e7e7d62850f14e5b6132101fc1edd19a16f/src/compiler/build_context/target_info.rs#L487
    // https://github.com/rust-lang/cargo/blob/98a09e7e7d62850f14e5b6132101fc1edd19a16f/src/compiler/mod.rs#L702
    let wasm = wasm[0];
    let mut files = BTreeSet::from([js.clone(), wasm.clone()]);
    let javascript = read(&js)?;
    for worker in [
        js.with_extension("worker.js"),
        wasm.with_extension("worker.js"),
    ] {
        let name = worker
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or("worker filename UTF-8")?;
        if filenames.contains(&worker) || javascript.contains(name) {
            // Current pthread output can reuse the main script. A separate worker
            // is included only when reported or referenced by this selected script.
            files.insert(worker);
        }
    }
    for path in &files {
        let actual = path
            .canonicalize()
            .map_err(|e| format!("missing browser artifact {}: {e}", path.display()))?;
        if !actual.starts_with(&root)
            || !actual.is_file()
            || fs::metadata(&actual).map_err(|e| e.to_string())?.len() == 0
        {
            return Err(format!(
                "browser artifact must be a nonempty file in the owned Cargo output: {}",
                path.display()
            ));
        }
    }
    Ok(BrowserArtifacts { js, files })
}

pub fn build(
    root: &Path,
    app: &App,
    p: &Profile,
    out: &Path,
    options: &Options,
    html: Option<&Path>,
    fix: bool,
) -> Result<()> {
    let mut deployment = deployment::validate(root, &app.manifest, p, "emscripten")?;
    options.validate_for_backend("emscripten")?;
    let out = build::prepare_output(root, out)?;
    for input in std::iter::once(&app.manifest)
        .chain(p.sources.iter())
        .map(PathBuf::as_path)
        .chain(html)
    {
        if input
            .canonicalize()
            .map_err(|e| e.to_string())?
            .starts_with(&out)
        {
            return Err("browser build inputs must be outside the owned output directory".into());
        }
    }
    clear_evidence(&out)?;
    let emcc_program = crate::find_program("emcc")?;
    let emcc = output(Command::new(&emcc_program).arg("--version"))?;
    if !emcc
        .lines()
        .next()
        .is_some_and(|line| line.split_whitespace().any(|word| word == "4.0.15"))
    {
        return Err("browser builds require Emscripten 4.0.15".into());
    }
    let compiler =
        output(Command::new("rustup").args(["which", "--toolchain", TOOLCHAIN, "rustc"]))?;
    let installed =
        PathBuf::from(output(Command::new(compiler.trim()).args(["--print", "sysroot"]))?.trim())
            .canonicalize()
            .map_err(|e| e.to_string())?;
    if installed.starts_with(&out) {
        return Err("installed browser SDK must be outside the owned output directory".into());
    }
    let cargo_config = env::var_os("NXRS_CARGO_CONFIG");
    let config_path = cargo_config.as_ref().map(|value| root.join(value));
    let config_hash = config_path
        .as_ref()
        .filter(|path| path.is_file())
        .map(|path| hash(path))
        .transpose()?;
    let cache_inputs = json!({
        "profile":p.json(), "profile_sources":deployment["profiles"],
        "application":app.manifest, "package":app.package, "binary":app.bin,
        "features":options.features, "no_default_features":options.no_default_features,
        "fix":fix, "toolchain":TOOLCHAIN, "target":TARGET,
        "sdk":sdk_identity(&installed, &emcc, &emcc_program)?,
        "controller":hash(&env::current_exe().map_err(|e| e.to_string())?)?,
        "rustflags":RUSTFLAGS, "emcc_cflags":EMCC_CFLAGS,
        "cargo_config":cargo_config, "cargo_config_sha256":config_hash,
    });
    let reused = begin_build(&out, &cache_inputs, fix, options.clean)?;
    let site = out.join("site");
    let receipts = out.join(STD_INPUTS);
    let sysroot = if fix {
        let path = out.join("toolchain");
        if !reused {
            tls_fix(&installed, &path, &receipts)?;
        }
        path
    } else {
        if !reused {
            write_json(
                &receipts.join("std-patch.json"),
                &json!({"mode":"upstream-unmodified"}),
            )?;
        }
        installed
    };
    copy_std_receipts(&receipts, &site, fix)?;
    let rustc = sysroot.join("bin/rustc");
    let cargo = sysroot.join("bin/cargo");
    if Path::new(output(Command::new(&rustc).args(["--print", "sysroot"]))?.trim())
        .canonicalize()
        .map_err(|e| e.to_string())?
        != sysroot
    {
        return Err("browser compiler selected another sysroot".into());
    }
    let mut command = Command::new(cargo);
    command
        .current_dir(root)
        .args(["build", "--locked", "--release", "--manifest-path"])
        .arg(&app.manifest)
        .args([
            "-p",
            &app.package,
            "--bin",
            &app.bin,
            "--target",
            TARGET,
            "-Zbuild-std=std,panic_abort",
            "--message-format=json-render-diagnostics",
        ])
        .env("NXRS_PLATFORM", p.name())
        .env("NXRS_SDK_ROOT", root)
        .env("RUSTUP_TOOLCHAIN", TOOLCHAIN)
        .env("CARGO_TARGET_DIR", out.join("cargo"))
        .env("EMCC_CFLAGS", EMCC_CFLAGS)
        .env("RUSTFLAGS", RUSTFLAGS)
        .env_remove("CARGO_ENCODED_RUSTFLAGS")
        .env("RUSTC", &rustc)
        .env("CARGO_BUILD_RUSTC", &rustc);
    options.configure_cargo(&mut command, &[]);
    if let Some(config) = cargo_config {
        command.arg("--config").arg(config);
    }
    let selection = crate::cargo_graph::resolve(&mut command, p)?;
    deployment["dependency_resolution"] = selection.json();
    deployment["model_features"] = json!(selection.model_features);
    write_json(&out.join("provider-selection.json"), &deployment)?;
    let messages = out.join("cargo-messages.jsonl");
    build::cargo_messages(&mut command, &messages)?;
    build::std_source(&messages, &sysroot, &rustc, &site)?;
    let artifact = selected_artifact(&read(&messages)?, app)?;
    let BrowserArtifacts { js, files } = browser_artifacts(&artifact, &out.join("cargo"))?;
    for path in files {
        reuse::copy_if_changed(&path, &site.join(path.file_name().unwrap()))?;
    }
    if let Some(html) = html {
        let text = read(html)?;
        if text.matches("__NXRS_SCRIPT__").count() != 1 {
            return Err("browser HTML template needs one __NXRS_SCRIPT__ marker".into());
        }
        reuse::write_if_changed(
            &site.join("index.html"),
            text.replace(
                "__NXRS_SCRIPT__",
                js.file_name()
                    .unwrap()
                    .to_str()
                    .ok_or("JS filename UTF-8")?,
            )
            .as_bytes(),
        )?;
    }
    let mut hashes = serde_json::Map::new();
    for entry in fs::read_dir(&site).map_err(|e| e.to_string())? {
        let path = entry.map_err(|e| e.to_string())?.path();
        if path.is_file() {
            hashes.insert(
                path.file_name().unwrap().to_string_lossy().into_owned(),
                json!(hash(&path)?),
            );
        }
    }
    let proof = crate::read_json(&site.join("std-source.json"))?;
    write_json(
        &site.join("manifest.json"),
        &json!({
            "source_sha":output(source::git(root).args(["rev-parse","HEAD"]))?.trim(),
            "rust":output(Command::new(rustc).args(["--version","--verbose"]))?.trim(),
            "emscripten":emcc.trim(), "rustflags":RUSTFLAGS, "emcc_cflags":EMCC_CFLAGS,
            "std_patch":crate::read_json(&site.join("std-patch.json"))?, "std_source":proof,
            "files":Value::Object(hashes), "app_manifest_sha256":hash(&app.manifest)?,
            "deployment":deployment, "cargo_artifact":artifact,
            "sdk_reused":reused, "build_inputs_sha256":crate::hash_bytes(&serde_json::to_vec(&cache_inputs).map_err(|e| e.to_string())?),
        }),
    )?;
    reuse::complete(&out, &cache_inputs, &cache_directories(fix))?;
    println!("Browser artifacts: {}", site.display());
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn thread_local_cleanup_selection_rejects_drift() {
        let guard = "            all(target_family = \"wasm\", not(target_env = \"p3\")),\n";
        let key = "            all(not(target_vendor = \"apple\"), not(target_family = \"wasm\"), target_family = \"unix\"),\n";
        let original = format!("{guard}{key}");
        let fixed = transform_tls(&original).unwrap();
        assert!(
            fixed.contains("not(all(target_os = \"emscripten\", target_feature = \"atomics\"))")
        );
        assert!(fixed.contains(&format!(
            "{key}            all(target_os = \"emscripten\", target_feature = \"atomics\"),\n"
        )));
        for bad in [
            guard.to_owned(),
            key.to_owned(),
            format!("{original}{guard}"),
            format!("{original}{key}"),
            fixed,
        ] {
            assert!(transform_tls(&bad).is_err());
        }
    }

    fn fixture_file(root: &Path, name: &str, contents: &str) -> PathBuf {
        let path = root.join(name);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, contents).unwrap();
        path
    }

    #[test]
    fn artifact_selection_uses_actual_cargo_package_and_binary_records_including_fresh_builds() {
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let mut outputs = Vec::new();
        let mut selected_app = None;
        for package in ["selected-app", "foreign-app"] {
            let directory = temp.0.join(package);
            let manifest = fixture_file(
                &directory,
                "Cargo.toml",
                &format!(
                    "[package]\nname={package:?}\nversion=\"0.1.0\"\nedition=\"2024\"\n[workspace]\n[[bin]]\nname=\"shared-bin\"\npath=\"src/main.rs\"\n"
                ),
            );
            fixture_file(&directory, "src/main.rs", "fn main() {}\n");
            let app = App {
                manifest,
                package: package.into(),
                bin: "shared-bin".into(),
                command: "fixture".into(),
                priority: 100,
                stack: 32768,
            };
            for fresh in [false, true] {
                let result = Command::new(env!("CARGO"))
                    .current_dir(&directory)
                    .args([
                        "build",
                        "--offline",
                        "--bin",
                        "shared-bin",
                        "--message-format=json",
                    ])
                    .env("CARGO_TARGET_DIR", directory.join("target"))
                    .env_remove("CARGO_BUILD_TARGET")
                    .env_remove("RUSTFLAGS")
                    .env_remove("CARGO_ENCODED_RUSTFLAGS")
                    .output()
                    .unwrap();
                assert!(
                    result.status.success(),
                    "{}",
                    String::from_utf8_lossy(&result.stderr)
                );
                let messages = String::from_utf8(result.stdout).unwrap();
                let artifact = selected_artifact(&messages, &app).unwrap();
                assert_eq!(artifact["fresh"], fresh);
                if fresh {
                    outputs.push(messages);
                }
            }
            if package == "selected-app" {
                selected_app = Some(app);
            }
        }
        let app = selected_app.unwrap();
        let artifact = selected_artifact(&outputs.join(""), &app).unwrap();
        assert_eq!(
            Path::new(artifact["manifest_path"].as_str().unwrap()),
            app.manifest
        );
        assert!(artifact["package_id"]
            .as_str()
            .unwrap()
            .contains("selected-app"));
        assert!(selected_artifact(&outputs[1], &app)
            .unwrap_err()
            .contains("found 0"));
        assert!(
            selected_artifact(&format!("{}{}", outputs[0], outputs[0]), &app)
                .unwrap_err()
                .contains("found 2")
        );
    }

    #[test]
    fn browser_files_come_from_the_selected_record_and_its_referenced_worker_only() {
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let cargo = temp.0.join("cargo");
        // Cargo preserves underscores in the WASM name even when the public JS
        // executable uses hyphens. These are packaging fixtures, not an SDK run.
        let js = fixture_file(
            &cargo,
            "release/selected-app.js",
            "load('selected_app.wasm'); new Worker('selected_app.worker.js');",
        );
        let wasm = fixture_file(&cargo, "release/selected_app.wasm", "fixture wasm bytes");
        let worker = fixture_file(&cargo, "release/selected_app.worker.js", "fixture worker");
        fixture_file(
            &cargo,
            "release/selected-app.worker.js",
            "stale same-app worker, no longer referenced",
        );
        fixture_file(&cargo, "release/other.js", "stale other program");
        fixture_file(&cargo, "release/other.wasm", "stale other wasm");
        fixture_file(&cargo, "release/other.worker.js", "stale other worker");
        fixture_file(
            &cargo,
            "release/deps/selected_app.wasm",
            "stale nested wasm",
        );
        let mut artifact = json!({"executable":js,"filenames":[js,wasm],"fresh":true});
        let selected = browser_artifacts(&artifact, &cargo).unwrap();
        assert_eq!(selected.js, js);
        assert_eq!(
            selected.files,
            BTreeSet::from([js.clone(), wasm.clone(), worker.clone()])
        );

        fs::write(
            &js,
            "load('selected_app.wasm'); /* pthreads reuse the main script */",
        )
        .unwrap();
        assert_eq!(
            browser_artifacts(&artifact, &cargo).unwrap().files,
            BTreeSet::from([js.clone(), wasm.clone()])
        );
        // Existing cached WASM must not fill in a missing Cargo filename.
        artifact["filenames"] = json!([js]);
        assert!(browser_artifacts(&artifact, &cargo)
            .unwrap_err()
            .contains("found 0"));
        artifact["filenames"] = json!([js, wasm, cargo.join("release/other.wasm")]);
        assert!(browser_artifacts(&artifact, &cargo)
            .unwrap_err()
            .contains("found 2"));
        artifact["filenames"] = json!([js, wasm]);
        fs::write(&js, "new Worker('selected_app.worker.js');").unwrap();
        fs::remove_file(worker).unwrap();
        assert!(browser_artifacts(&artifact, &cargo)
            .unwrap_err()
            .contains("missing browser artifact"));
    }

    fn retained_outputs(out: &Path) {
        fixture_file(out, "cargo/object", "Cargo compilation state");
        fixture_file(out, "toolchain/lib/source.rs", "private SDK contents");
        fixture_file(out, "std-inputs/std-patch.json", "{\"mode\":\"fixture\"}");
        fixture_file(out, "std-inputs/std-tls.patch", "fixture patch receipt");
        fixture_file(out, "site/manifest.json", "previous success");
        fixture_file(out, "site/old-app.js", "previous artifact");
        fixture_file(out, "provider-selection.json", "previous selection");
        fixture_file(out, "cargo-messages.jsonl", "previous compiler output");
    }

    #[test]
    fn compatible_rebuild_preserves_sdk_and_cargo_but_regenerates_site_and_receipts() {
        let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let out = &temp.0;
        let inputs = json!({"sdk":"fixture","profile":"same","features":[]});
        retained_outputs(out);
        let sdk_time = out
            .join("toolchain/lib/source.rs")
            .metadata()
            .unwrap()
            .modified()
            .unwrap();
        let cargo_time = out
            .join("cargo/object")
            .metadata()
            .unwrap()
            .modified()
            .unwrap();
        reuse::complete(out, &inputs, &cache_directories(true)).unwrap();
        assert!(begin_build(out, &inputs, true, false).unwrap());
        assert_eq!(
            out.join("toolchain/lib/source.rs")
                .metadata()
                .unwrap()
                .modified()
                .unwrap(),
            sdk_time
        );
        assert_eq!(
            out.join("cargo/object")
                .metadata()
                .unwrap()
                .modified()
                .unwrap(),
            cargo_time
        );
        assert_eq!(fs::read_dir(out.join("site")).unwrap().count(), 0);
        assert!(!out.join("provider-selection.json").exists());
        assert!(!out.join("cargo-messages.jsonl").exists());
        copy_std_receipts(&out.join(STD_INPUTS), &out.join("site"), true).unwrap();
        assert_eq!(
            fs::read(out.join("site/std-tls.patch")).unwrap(),
            fs::read(out.join("std-inputs/std-tls.patch")).unwrap()
        );
        assert!(!out.join("site/manifest.json").exists());
        // An incomplete attempt cannot authorize the next SDK/Cargo reuse.
        assert!(!begin_build(out, &inputs, true, false).unwrap());
        assert!(!out.join("cargo/object").exists());
    }

    #[test]
    fn changed_sdk_bytes_receipts_options_and_clean_invalidate_browser_reuse() {
        for change in [
            "compiler",
            "source",
            "emcc-version",
            "emcc-program",
            "receipt",
            "features",
            "clean",
            "mode",
        ] {
            let temp = crate::TempDir::new(&std::env::temp_dir()).unwrap();
            let installed = temp.0.join("installed");
            let compiler = fixture_file(&installed, "bin/rustc", "compiler bytes");
            let source = fixture_file(&installed, TLS, "standard library source");
            let emcc = fixture_file(&temp.0, "emcc", "emcc driver bytes");
            let out = temp.0.join("out");
            retained_outputs(&out);
            let mut inputs = json!({"sdk":sdk_identity(&installed, "fixture version one", &emcc).unwrap(),"fix":true,"features":[]});
            reuse::complete(&out, &inputs, &cache_directories(true)).unwrap();
            let mut fix = true;
            match change {
                "compiler" => fs::write(compiler, "changed compiler bytes").unwrap(),
                "source" => fs::write(source, "changed std source at the same path").unwrap(),
                "emcc-program" => fs::write(&emcc, "changed emcc driver bytes").unwrap(),
                "receipt" => fs::write(
                    out.join("std-inputs/std-tls.patch"),
                    "modified cached receipt",
                )
                .unwrap(),
                "features" => inputs["features"] = json!(["another"]),
                "mode" => {
                    fix = false;
                    inputs["fix"] = json!(false);
                }
                _ => {}
            }
            inputs["sdk"] = sdk_identity(
                &installed,
                if change == "emcc-version" {
                    "fixture version two"
                } else {
                    "fixture version one"
                },
                &emcc,
            )
            .unwrap();
            assert!(
                !begin_build(&out, &inputs, fix, change == "clean").unwrap(),
                "{change}"
            );
            assert!(!out.join("cargo/object").exists(), "{change}");
            assert!(!out.join("toolchain").exists(), "{change}");
            assert!(!out.join("site/manifest.json").exists(), "{change}");
        }
    }
}
