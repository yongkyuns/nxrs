//! Relocatable Rust input for the final NuttX link; only native libc is deferred.
use crate::{hash, output, run, write_json, Result};
use serde_json::json;
use std::{
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};

pub fn arguments(args: &[String]) -> Result<(Vec<String>, Vec<String>)> {
    let mut kept = vec![];
    let mut deferred = vec![];
    let mut args = args.iter();
    while let Some(arg) = args.next() {
        let arg = if arg == "-l" {
            format!("-l{}", args.next().ok_or("missing -l argument")?)
        } else {
            arg.clone()
        };
        if ["-lc", "-lm", "-lpthread"].contains(&arg.as_str()) {
            deferred.push(arg)
        } else {
            kept.push(arg)
        }
    }
    kept.extend(["-r", "--gc-sections", "-u", "main"].map(str::to_owned));
    Ok((kept, deferred))
}
pub fn link(original: &[String]) -> Result<()> {
    let root = PathBuf::from(env::var_os("NUTTX_STD_SYSROOT").ok_or("missing private sysroot")?);
    let rustc = root.join("bin/rustc");
    if output(Command::new(&rustc).args(["--print", "sysroot"]))?.trim()
        != root.to_str().ok_or("sysroot must be UTF-8")?
    {
        return Err("selected Rust sysroot differs".into());
    }
    let version = output(Command::new(&rustc).arg("-vV"))?;
    let host = version
        .lines()
        .find_map(|line| line.strip_prefix("host: "))
        .ok_or("rustc host missing")?;
    let (mut args, deferred) = arguments(original)?;
    let log = PathBuf::from(env::var_os("NUTTX_STD_LINK_LOG").ok_or("missing link evidence path")?);
    let evidence = log.parent().ok_or("link evidence parent")?;
    let gnu = env::var_os("NUTTX_STD_GNU_LINKER");
    let linker = if let Some(path) = &gnu {
        let path = PathBuf::from(path);
        if !path.is_absolute()
            || !path.is_file()
            || path.file_name().unwrap() != "xtensa-esp32s3-elf-ld"
            || args.iter().any(|s| s == "-flavor")
        {
            return Err("only the selected Xtensa GNU linker is allowed".into());
        }
        args.extend([
            format!("-Map={}", evidence.join("rust-partial.map").display()),
            "--cref".into(),
        ]);
        path
    } else {
        if !args.iter().any(|s| s == "-flavor") {
            args.splice(0..0, ["-flavor".into(), "gnu".into()]);
        }
        root.join("lib/rustlib").join(host).join("bin/rust-lld")
    };
    write_json(
        &log,
        &json!({"linker":linker,"original":original,"effective":args,"deferred_nuttx_libraries":deferred}),
    )?;
    run(Command::new(&linker).args(&args))?;
    if gnu.is_some() {
        let binary = PathBuf::from(
            args.windows(2)
                .find(|pair| pair[0] == "-o")
                .ok_or("link output missing")?[1]
                .clone(),
        );
        fs::copy(&binary, evidence.join("rust-input-before.elf")).map_err(|e| e.to_string())?;
        let prefix = linker
            .to_str()
            .ok_or("linker path must be UTF-8")?
            .strip_suffix("ld")
            .unwrap();
        prune(&binary, prefix, evidence)?;
        fs::copy(&binary, evidence.join("rust-input.elf")).map_err(|e| e.to_string())?;
        let table = output(
            Command::new(format!("{prefix}readelf"))
                .args(["-W", "-S", "-r"])
                .arg(&binary),
        )?;
        fs::write(evidence.join("rust-relocations.txt"), table).map_err(|e| e.to_string())?;
    }
    Ok(())
}
pub fn prune(binary: &Path, prefix: &str, evidence: &Path) -> Result<()> {
    let table = output(
        Command::new(format!("{prefix}readelf"))
            .env("LC_ALL", "C")
            .args(["-W", "-s"])
            .arg(binary),
    )?;
    fs::write(evidence.join("rust-symbols-before.txt"), &table).map_err(|e| e.to_string())?;
    let mut names = std::collections::BTreeSet::new();
    for line in table.lines() {
        let p: Vec<_> = line.split_whitespace().collect();
        if p.len() == 8
            && p[0]
                .strip_suffix(':')
                .is_some_and(|s| s.parse::<u64>().is_ok())
            && p[4] == "LOCAL"
            && p[6] == "UND"
        {
            if p[7].contains('#') {
                return Err("unsupported local symbol spelling".into());
            }
            names.insert(p[7]);
        }
    }
    let candidates = evidence.join("discarded-undefined.txt");
    fs::write(
        &candidates,
        names.iter().map(|s| format!("{s}\n")).collect::<String>(),
    )
    .map_err(|e| e.to_string())?;
    let before = hash(binary)?;
    if !names.is_empty() {
        let fresh = binary.with_extension("pruned");
        let result = output(
            Command::new(format!("{prefix}objcopy"))
                .arg(format!("--strip-symbols={}", candidates.display()))
                .arg(binary)
                .arg(&fresh),
        )
        .and_then(|_| fs::rename(&fresh, binary).map_err(|e| e.to_string()));
        let _ = fs::remove_file(fresh);
        result?;
    }
    write_json(
        &evidence.join("rust-symbol-pruning.json"),
        &json!({"removed_local_undefined":names,"before_sha256":before,"after_sha256":hash(binary)?,"policy":"local undefined only; objcopy rejects remaining relocation references"}),
    )?;
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_native_libc_is_deferred() {
        let args = [
            "a.o",
            "-l",
            "c",
            "-lm",
            "-lpthread",
            "-lgcc",
            "--start-group",
        ]
        .map(str::to_owned);
        let (k, d) = arguments(&args).unwrap();
        assert_eq!(d, ["-lc", "-lm", "-lpthread"]);
        assert_eq!(
            k,
            [
                "a.o",
                "-lgcc",
                "--start-group",
                "-r",
                "--gc-sections",
                "-u",
                "main"
            ]
        );
        assert!(arguments(&["-l".into()]).is_err());
    }
}
