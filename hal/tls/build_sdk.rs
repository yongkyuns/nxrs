//! Build the pinned TLS SDK against the same private ABI/configuration headers.
use nxrs_build::{archive, find_program, hash, native, output, read_json, Result};
use std::{env, fs, path::Path, process::Command};

pub(crate) fn nuttx_headers(include: &Path) -> Result<()> {
    let config = include.join("nuttx/config.h");
    // Explicitly observe deletion of a required file: on Windows, directory
    // timestamp tracking alone may not invalidate a successful build script.
    println!("cargo:rerun-if-changed={}", config.display());
    // Keep recursive tracking for other configured NuttX and ABI headers.
    println!("cargo:rerun-if-changed={}", include.display());
    if !config.is_file() {
        return Err("Configured NuttX target headers required".into());
    }
    Ok(())
}

pub fn build(here: &Path, out: &Path, target: &str) -> Result<()> {
    let root = here.join("../..");
    let pin = read_json(&root.join("upstream/mbedtls/source.json"))?;
    let archive = env::var_os("NXRS_TLS_ARCHIVE")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| root.join("target/mbedtls-4.2.0.tar.bz2"));
    if !archive.is_file()
        || archive.metadata().map_err(|e| e.to_string())?.len()
            != pin["size"].as_u64().ok_or("invalid TLS source size")?
        || hash(&archive)? != pin["sha256"].as_str().ok_or("invalid TLS source hash")?
    {
        return Err("Verified TLS source archive required".into());
    }
    println!("cargo:rerun-if-changed={}", archive.display());
    fs::create_dir_all(out).map_err(|e| e.to_string())?;
    let source = out.join(format!(
        "mbedtls-{}",
        pin["version"].as_str().ok_or("invalid TLS version")?
    ));
    if !source.exists() {
        archive::bzip2(&archive, &source)?;
    }
    let ffi = here.join("src/sdk/ffi");
    let mut compiler = env::var("CC").unwrap_or("cc".into());
    let mut archiver = env::var("AR").unwrap_or("ar".into());
    let mut flags = nxrs_build::split_flags(&env::var("CFLAGS").unwrap_or_default())?;
    flags.extend(["-ffunction-sections".into(), "-fdata-sections".into()]);
    let mut cross = vec![];
    if target.contains("nuttx") {
        let (prefix, cpu) = if target.starts_with("thumbv8m.main-") {
            (
                "arm-none-eabi-",
                vec!["-mcpu=cortex-m33", "-mthumb", "-mfloat-abi=soft"],
            )
        } else if target.starts_with("xtensa-esp32s3-") {
            ("xtensa-esp32s3-elf-", vec!["-mlongcalls"])
        } else {
            return Err("Unsupported NuttX TLS target".into());
        };
        compiler = env::var("CC").unwrap_or(format!("{prefix}gcc"));
        archiver = env::var("AR").unwrap_or(format!("{prefix}ar"));
        let include = std::path::PathBuf::from(
            env::var_os("NXRS_NUTTX_INCLUDE").ok_or("configured NuttX target headers required")?,
        );
        nuttx_headers(&include)?;
        flags.extend(cpu.iter().map(|s| s.to_string()));
        flags.extend(["-D__NuttX__".into(), format!("-I{}", include.display())]);
        cross = vec![
            "-DCMAKE_SYSTEM_NAME=Generic",
            "-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY",
        ];
    }
    let compiler = find_program(&compiler)?;
    let compiler = compiler.to_str().ok_or("C compiler path must be UTF-8")?;
    let archiver = find_program(&archiver)?;
    let archiver = archiver.to_str().ok_or("archiver path must be UTF-8")?;
    let directory = out.join("sdk");
    nxrs_build::remove_file(&directory.join("CMakeCache.txt"))?;
    nxrs_build::remove_dir(&directory.join("CMakeFiles"))?;
    let configs = [
        format!(
            "-DMBEDTLS_CONFIG_FILE=\"{}\"",
            ffi.join("mbedtls_config.h").display()
        ),
        format!(
            "-DTF_PSA_CRYPTO_CONFIG_FILE=\"{}\"",
            ffi.join("crypto_config.h").display()
        ),
    ];
    let mut cmake = native::command("CMAKE", "cmake")?;
    output(
        cmake
            .arg("-S")
            .arg(&source)
            .arg("-B")
            .arg(&directory)
            .args([
                "-DENABLE_TESTING=OFF",
                "-DENABLE_PROGRAMS=OFF",
                "-DGEN_FILES=OFF",
                "-DCMAKE_BUILD_TYPE=MinSizeRel",
                "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
            ])
            .arg(format!("-DCMAKE_C_COMPILER={compiler}"))
            .arg(format!("-DCMAKE_AR={archiver}"))
            .arg(format!(
                "-DCMAKE_C_FLAGS={}",
                flags
                    .iter()
                    .map(|s| nxrs_build::shell_quote(s))
                    .collect::<Vec<_>>()
                    .join(" ")
            ))
            .args(cross)
            .arg(format!(
                "-DMBEDTLS_CONFIG_FILE={}",
                ffi.join("mbedtls_config.h").display()
            ))
            .arg(format!(
                "-DTF_PSA_CRYPTO_CONFIG_FILE={}",
                ffi.join("crypto_config.h").display()
            )),
    )?;
    output(
        native::command("CMAKE", "cmake")?
            .arg("--build")
            .arg(&directory)
            .args(["--target", "mbedtls", "-j", "4"]),
    )?;
    let includes = [
        source.join("include"),
        source.join("tf-psa-crypto/include"),
        source.join("tf-psa-crypto/drivers/builtin/include"),
    ];
    let mut args = flags;
    args.extend(
        ["-std=c11", "-Wall", "-Wextra", "-Werror"]
            .iter()
            .map(|s| s.to_string()),
    );
    args.extend(includes.iter().map(|p| format!("-I{}", p.display())));
    args.extend(configs);
    let mut sources = vec![(ffi.join("sdk.c"), out.join("sdk.o"))];
    if env::var_os("CARGO_FEATURE_PROBE").is_some() {
        sources.push((root.join("tests/tls/ffi/control.c"), out.join("control.o")))
    }
    for (source, object) in &sources {
        let mut a = args.clone();
        a.extend([
            "-c".into(),
            source.to_string_lossy().into(),
            "-o".into(),
            object.to_string_lossy().into(),
        ]);
        output(Command::new(compiler).args(a))?;
    }
    let native = out.join("libtls_abi.a");
    nxrs_build::remove_file(&native)?;
    output(
        Command::new(archiver)
            .arg("rcs")
            .arg(native)
            .args(sources.iter().map(|(_, o)| o)),
    )?;
    for path in [
        out.to_path_buf(),
        directory.join("library"),
        directory.join("tf-psa-crypto/core"),
    ] {
        println!("cargo:rustc-link-search=native={}", path.display())
    }
    for name in ["tls_abi", "mbedtls", "mbedx509", "tfpsacrypto"] {
        println!("cargo:rustc-link-lib=static={name}")
    }
    Ok(())
}
