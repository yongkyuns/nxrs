//! Compile actual downstream crates, including the negative API contracts.
//! `cargo check` must reject the order itself; no monomorphized const assertion
//! or runtime-only failure is accepted as a compile-time ordering check.
use std::{env, fs, path::PathBuf, process::Command};

struct Fixture(PathBuf);

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn downstream_compile_contracts() {
    let root = env::temp_dir().join(format!("nxrs-lock-order-compile-{}", std::process::id()));
    fs::create_dir(&root).unwrap();
    let fixture = Fixture(root);
    fs::create_dir_all(fixture.0.join("src/bin")).unwrap();
    let package = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let path = package
        .to_str()
        .unwrap()
        .replace('\\', "\\\\")
        .replace('"', "\\\"");
    fs::write(
        fixture.0.join("Cargo.toml"),
        format!(
            "[workspace]\n[package]\nname='lock-order-consumer'\nversion='0.0.0'\nedition='2021'\n\
         [dependencies]\nlock_order={{package='nxrs-lock-order',path=\"{path}\"}}\n"
        ),
    )
    .unwrap();

    let cases: &[(&str, &str, Option<&str>)] = &[
        ("ordered", include_str!("compile/ordered.rs"), None),
        ("reverse", include_str!("compile/reverse.rs"), Some("E0277")),
        (
            "reacquire",
            include_str!("compile/reacquire.rs"),
            Some("E0277"),
        ),
        (
            "equal_rank",
            include_str!("compile/equal_rank.rs"),
            Some("E0277"),
        ),
        (
            "parent_reuse",
            include_str!("compile/parent_reuse.rs"),
            Some("borrow"),
        ),
        (
            "data_escape",
            include_str!("compile/data_escape.rs"),
            Some("lifetime"),
        ),
        (
            "context_escape",
            include_str!("compile/context_escape.rs"),
            Some("lifetime"),
        ),
        (
            "root_escape",
            include_str!("compile/root_escape.rs"),
            Some("lifetime"),
        ),
        (
            "context_send",
            include_str!("compile/context_send.rs"),
            Some("E0277"),
        ),
        (
            "context_sync",
            include_str!("compile/context_sync.rs"),
            Some("E0277"),
        ),
        (
            "non_send_value",
            include_str!("compile/non_send_value.rs"),
            Some("E0277"),
        ),
        (
            "forge_context",
            include_str!("compile/forge_context.rs"),
            Some("private"),
        ),
        (
            "forge_rank",
            include_str!("compile/forge_rank.rs"),
            Some("E0277"),
        ),
        (
            "forge_order",
            include_str!("compile/forge_order.rs"),
            Some("E0117"),
        ),
        (
            "native_access",
            include_str!("compile/native_access.rs"),
            Some("private"),
        ),
    ];
    let cargo = env::var_os("CARGO").unwrap_or_else(|| "cargo".into());
    for (name, source, expected) in cases {
        fs::write(fixture.0.join(format!("src/bin/{name}.rs")), source).unwrap();
        let output = Command::new(&cargo)
            .current_dir(&fixture.0)
            .args(["check", "--offline", "--quiet", "--bin", name])
            .env("CARGO_TARGET_DIR", fixture.0.join("target"))
            .env("CARGO_PROFILE_DEV_DEBUG", "0")
            .env("CARGO_INCREMENTAL", "0")
            .env_remove("CARGO_BUILD_TARGET")
            .env_remove("RUSTFLAGS")
            .env_remove("CARGO_ENCODED_RUSTFLAGS")
            .output()
            .unwrap();
        let diagnostic = String::from_utf8_lossy(&output.stderr);
        match expected {
            None => assert!(
                output.status.success(),
                "{name} must compile:\n{diagnostic}"
            ),
            Some(message) => {
                assert!(!output.status.success(), "{name} unexpectedly compiled");
                assert!(
                    diagnostic.contains(message),
                    "{name} failed for another reason:\n{diagnostic}"
                );
            }
        }
    }
}
