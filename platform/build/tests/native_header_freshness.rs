//! Cargo must revisit native adapters when configured headers change in place.
use std::{fs, path::Path, process::Command};

fn cargo(fixture: &Path, headers: &Path, detector: bool) -> std::process::Output {
    let mut command = Command::new(env!("CARGO"));
    command
        .current_dir(fixture)
        .args(["build", "--offline", "--message-format=json"])
        .env("CARGO_TARGET_DIR", fixture.join("target"))
        .env("NXRS_NUTTX_INCLUDE", headers)
        .env_remove("CARGO_BUILD_TARGET")
        .env_remove("RUSTFLAGS")
        .env_remove("CARGO_ENCODED_RUSTFLAGS");
    if detector {
        command.args(["--features", "detector"]);
    }
    command.output().unwrap()
}

fn build_count(fixture: &Path, headers: &Path, detector: bool) -> usize {
    let output = cargo(fixture, headers, detector);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let record = String::from_utf8(output.stdout)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str::<serde_json::Value>(line).unwrap())
        .find(|row| {
            row["reason"] == "build-script-executed"
                && row["package_id"]
                    .as_str()
                    .is_some_and(|id| id.contains("native-header-fixture"))
        })
        .expect("fixture build script output");
    let out = Path::new(record["out_dir"].as_str().unwrap());
    fs::read_to_string(out.join("runs"))
        .unwrap()
        .lines()
        .count()
}

#[test]
fn native_adapters_track_same_path_header_changes() {
    let temp = nxrs_build::TempDir::new(&std::env::temp_dir()).unwrap();
    let fixture = temp.0.join("fixture");
    let headers = temp.0.join("headers");
    fs::create_dir_all(fixture.join("src")).unwrap();
    fs::create_dir_all(headers.join("nuttx")).unwrap();
    fs::create_dir_all(headers.join("sys")).unwrap();
    #[cfg(unix)]
    {
        fs::create_dir(temp.0.join("arch")).unwrap();
        std::os::unix::fs::symlink(temp.0.join("arch"), headers.join("arch")).unwrap();
    }
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    fs::write(
        fixture.join("Cargo.toml"),
        format!(
            "[package]\nname = \"native-header-fixture\"\nversion = \"0.0.0\"\nedition = \"2021\"\n\
             [features]\ndetector = []\n\
             [build-dependencies]\nnxrs-build-support = {{ path = {:?} }}\n\
             nxrs-detector-build-support = {{ path = {:?} }}\n\
             [workspace]\n",
            root.join("../build-support"),
            root.join("../../hal/detector/build-support"),
        ),
    )
    .unwrap();
    fs::write(fixture.join("src/lib.rs"), "pub fn fixture() {}\n").unwrap();
    fs::write(
        fixture.join("build.rs"),
        format!(
            r#"
#[allow(dead_code)]
#[path = {tls:?}]
mod tls;
use nxrs_detector_build_support as detector;
fn main() {{
    use std::io::Write;
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-env-changed=NXRS_NUTTX_INCLUDE");
    let headers = std::path::PathBuf::from(std::env::var_os("NXRS_NUTTX_INCLUDE").unwrap());
    if std::env::var_os("CARGO_FEATURE_DETECTOR").is_some() {{
        detector::nuttx_headers(&headers).unwrap();
    }} else {{
        tls::nuttx_headers(&headers).unwrap();
    }}
    let out = std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
    let mut runs = std::fs::OpenOptions::new().create(true).append(true).open(out.join("runs")).unwrap();
    writeln!(runs, "run").unwrap();
}}
"#,
            tls = root.join("../../hal/tls/build_sdk.rs"),
        ),
    )
    .unwrap();

    let config = headers.join("nuttx/config.h");
    let abi = headers.join("sys/abi.h");
    let qualified = "#define CONFIG_ARCH_CHIP_ESP32S3 1\n#define CONFIG_BUILD_FLAT 1\n";
    for detector in [false, true] {
        fs::write(&config, qualified).unwrap();
        fs::write(&abi, "#define ABI_VERSION 1\n").unwrap();
        #[cfg(unix)]
        fs::write(headers.join("arch/abi.h"), "#define ARCH_ABI_VERSION 1\n").unwrap();
        assert_eq!(build_count(&fixture, &headers, detector), 1);
        assert_eq!(build_count(&fixture, &headers, detector), 1);

        // Keep both the include environment value and header paths unchanged.
        fs::write(&config, format!("{qualified}#define CONFIG_TLS_NELEM 32\n")).unwrap();
        assert_eq!(build_count(&fixture, &headers, detector), 2);
        assert_eq!(build_count(&fixture, &headers, detector), 2);

        fs::write(&abi, "#define ABI_VERSION 2\n").unwrap();
        assert_eq!(build_count(&fixture, &headers, detector), 3);

        // NuttX exposes architecture/board headers through include-tree symlinks.
        #[cfg(unix)]
        {
            fs::write(headers.join("arch/abi.h"), "#define ARCH_ABI_VERSION 2\n").unwrap();
            assert_eq!(build_count(&fixture, &headers, detector), 4);
        }

        if detector {
            fs::write(&config, format!("{qualified}#define CONFIG_SMP 1\n")).unwrap();
            let output = cargo(&fixture, &headers, true);
            assert!(!output.status.success());
            assert!(String::from_utf8_lossy(&output.stderr).contains("SMP-safe perf clock"));
        } else {
            fs::remove_file(&config).unwrap();
            let output = cargo(&fixture, &headers, false);
            assert!(!output.status.success());
            assert!(String::from_utf8_lossy(&output.stderr)
                .contains("Configured NuttX target headers required"));
        }
    }
}
