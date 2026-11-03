//! Browser std-thread builds using the pinned Emscripten SDK and a private Rust SDK.
use crate::{
    build::{self, App, Options},
    hash, output,
    profile::Profile,
    read, source, write_json, Result,
};
use serde_json::{json, Value};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};
const TOOLCHAIN: &str = "nightly-2026-09-25";
const TARGET: &str = "wasm32-unknown-emscripten";
const TLS: &str = "lib/rustlib/src/rust/library/std/src/sys/thread_local/mod.rs";
fn transform_tls(text: &str) -> Result<String> {
    let guard = "            all(target_family = \"wasm\", not(target_env = \"p3\")),\n";
    let key="            all(not(target_vendor = \"apple\"), not(target_family = \"wasm\"), target_family = \"unix\"),\n";
    if text.matches(guard).count() != 1 || text.matches(key).count() != 1 {
        return Err("unexpected browser TLS selection layout".into());
    }
    let changed=text.replace(guard,"            all(\n                target_family = \"wasm\",\n                not(target_env = \"p3\"),\n                not(all(target_os = \"emscripten\", target_feature = \"atomics\")),\n            ),\n").replace(key,&format!("{key}            all(target_os = \"emscripten\", target_feature = \"atomics\"),\n"));
    Ok(changed)
}
fn tls_fix(source: &Path, dest: &Path, site: &Path) -> Result<()> {
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
    fs::write(&path, changed).map_err(|e| e.to_string())?;
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
    fs::write(site.join("std-tls.patch"), diff.stdout).map_err(|e| e.to_string())?;
    write_json(
        &site.join("std-patch.json"),
        &json!({"mode":"emscripten-atomic-tls-key-cleanup","source_path":TLS,"original_blob":before.trim(),"patched_blob":output(Command::new("git").arg("hash-object").arg(path))?.trim(),"installed_toolchain_unchanged":true}),
    )
}
pub fn prepare_std(source: &Path, out: &Path) -> Result<()> {
    let site = out.join("site");
    fs::create_dir_all(&site).map_err(|e| e.to_string())?;
    tls_fix(source, &out.join("toolchain"), &site)
}
fn files(path: &Path, result: &mut Vec<PathBuf>) -> Result<()> {
    for entry in fs::read_dir(path).map_err(|e| e.to_string())? {
        let p = entry.map_err(|e| e.to_string())?.path();
        if p.is_dir() {
            files(&p, result)?;
        } else {
            result.push(p);
        }
    }
    Ok(())
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
    p.validate_target("emscripten")?;
    options.validate()?;
    if !options.helpers.is_empty()
        || !options.enable.is_empty()
        || !options.disable.is_empty()
        || options.abi.is_some()
        || options.size
        || options.trace
        || options.immediate
    {
        return Err("NuttX switches cannot configure browser builds".into());
    }
    let emcc = output(Command::new("emcc").arg("--version"))?;
    if !emcc.lines().next().is_some_and(|s| s.contains("4.0.15")) {
        return Err("browser builds require Emscripten 4.0.15".into());
    }
    let rustc = output(Command::new("rustup").args(["which", "--toolchain", TOOLCHAIN, "rustc"]))?;
    let installed =
        PathBuf::from(output(Command::new(rustc.trim()).args(["--print", "sysroot"]))?.trim());
    let out = build::prepare_output(root, out)?;
    for name in ["site", "cargo", "toolchain"] {
        crate::remove_dir(&out.join(name))?;
    }
    let site = out.join("site");
    fs::create_dir(&site).map_err(|e| e.to_string())?;
    let sysroot = if fix {
        let path = out.join("toolchain");
        tls_fix(&installed, &path, &site)?;
        path
    } else {
        write_json(
            &site.join("std-patch.json"),
            &json!({"mode":"upstream-unmodified"}),
        )?;
        installed
    };
    let rustc = sysroot.join("bin/rustc");
    let cargo = sysroot.join("bin/cargo");
    if output(Command::new(&rustc).args(["--print", "sysroot"]))?.trim()
        != sysroot.to_str().ok_or("SDK path UTF-8")?
    {
        return Err("browser compiler selected another sysroot".into());
    }
    let flags="-C target-feature=+atomics,+bulk-memory,+mutable-globals -C panic=abort -C link-arg=-pthread -C link-arg=-sPROXY_TO_PTHREAD=1 -C link-arg=-sPTHREAD_POOL_SIZE=4 -C link-arg=-sPTHREAD_POOL_SIZE_STRICT=2 -C link-arg=-sALLOW_BLOCKING_ON_MAIN_THREAD=0 -C link-arg=-sEXIT_RUNTIME=1 -C link-arg=-sINITIAL_MEMORY=67108864 -C link-arg=-sALLOW_MEMORY_GROWTH=0 -C link-arg=-sSTACK_SIZE=2097152 -C link-arg=-sDEFAULT_PTHREAD_STACK_SIZE=2097152 -C link-arg=-sASSERTIONS=2 -C link-arg=-sENVIRONMENT=web,worker";
    let mut command = Command::new(cargo);
    command
        .current_dir(root)
        .args(["build", "--locked", "--release", "--manifest-path"])
        .arg(&app.manifest)
        .args([
            "--bin",
            &app.bin,
            "--target",
            TARGET,
            "-Zbuild-std=std,panic_abort",
            "--message-format=json-render-diagnostics",
        ])
        .env("NXRS_PLATFORM", p.name())
        .env("RUSTUP_TOOLCHAIN", TOOLCHAIN)
        .env("CARGO_TARGET_DIR", out.join("cargo"))
        .env("EMCC_CFLAGS", "-pthread")
        .env("RUSTFLAGS", flags)
        .env_remove("CARGO_ENCODED_RUSTFLAGS")
        .env("RUSTC", &rustc)
        .env("CARGO_BUILD_RUSTC", &rustc);
    if !options.features.is_empty() {
        command.args(["--features", &options.features.join(",")]);
    }
    build::cargo_messages(&mut command, &out.join("cargo-messages.jsonl"))?;
    build::std_source(&out.join("cargo-messages.jsonl"), &sysroot, &rustc, &site)?;
    let mut emitted = vec![];
    files(
        &out.join("cargo").join(TARGET).join("release"),
        &mut emitted,
    )?;
    let pairs: Vec<_> = emitted
        .iter()
        .filter(|p| p.extension().is_some_and(|s| s == "wasm") && p.with_extension("js").is_file())
        .collect();
    let mut digests = std::collections::BTreeSet::new();
    for wasm in &pairs {
        digests.insert((hash(wasm)?, hash(&wasm.with_extension("js"))?));
    }
    if pairs.is_empty() || digests.len() != 1 {
        return Err("missing or ambiguous emitted JS/WASM pair".into());
    }
    let wasm = pairs[0];
    let js = wasm.with_extension("js");
    let mut selected = vec![wasm.clone(), js.clone()];
    for entry in fs::read_dir(js.parent().unwrap()).map_err(|e| e.to_string())? {
        let p = entry.map_err(|e| e.to_string())?.path();
        if p.file_name()
            .is_some_and(|s| s.to_string_lossy().ends_with(".worker.js"))
        {
            selected.push(p);
        }
    }
    for path in selected {
        fs::copy(&path, site.join(path.file_name().unwrap())).map_err(|e| e.to_string())?;
    }
    if let Some(html) = html {
        let text = read(html)?;
        if text.matches("__NXRS_SCRIPT__").count() != 1 {
            return Err("browser HTML template needs one __NXRS_SCRIPT__ marker".into());
        }
        fs::write(
            site.join("index.html"),
            text.replace(
                "__NXRS_SCRIPT__",
                js.file_name()
                    .unwrap()
                    .to_str()
                    .ok_or("JS filename UTF-8")?,
            ),
        )
        .map_err(|e| e.to_string())?;
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
        &json!({"source_sha":output(source::git(root).args(["rev-parse","HEAD"]))?.trim(),"rust":output(Command::new(rustc).args(["--version","--verbose"]))?.trim(),"emscripten":emcc.trim(),"rustflags":flags,"emcc_cflags":"-pthread","std_patch":crate::read_json(&site.join("std-patch.json"))?,"std_source":proof,"files":Value::Object(hashes),"app_manifest_sha256":hash(&app.manifest)?}),
    )?;
    println!("Browser artifacts: {}", site.display());
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn thread_local_cleanup_selection_rejects_drift() {
        let guard = "            all(target_family = \"wasm\", not(target_env = \"p3\")),\n";
        let key="            all(not(target_vendor = \"apple\"), not(target_family = \"wasm\"), target_family = \"unix\"),\n";
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
}
