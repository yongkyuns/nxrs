//! Real Cargo selection tests. No mock resolver and no target SDK required.
use nxrs_build::{build::Options, selection::{host_target, CargoSelection}, TempDir};
use std::{fs, path::Path, process::Command};

fn put(path: &Path, content: &str) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, content).unwrap();
}
fn cargo(args: &[&str], root: &Path) {
    let output = Command::new("cargo").current_dir(root).args(args)
        .env_remove("NXRS_PLATFORM").output().unwrap();
    assert!(output.status.success(), "{args:?}: {}", String::from_utf8_lossy(&output.stderr));
}

#[test]
fn optional_alias_default_features_and_inactive_target_follow_actual_cargo_graph() {
    let temp = TempDir::new(&std::env::temp_dir()).unwrap();
    let root = &temp.0;
    put(&root.join("Cargo.toml"), r#"[workspace]
resolver = "2"
members = ["app", "detector", "imu"]
[workspace.dependencies]
sensor_alias = { package = "nxrs-detector", path = "detector" }
"#);
    for (folder, name) in [("detector", "nxrs-detector"), ("imu", "nxrs-imu")] {
        put(&root.join(format!("{folder}/Cargo.toml")),
            &format!("[package]\nname = \"{name}\"\nversion = \"0.1.0\"\nedition = \"2021\"\n"));
        put(&root.join(format!("{folder}/src/lib.rs")), "pub fn live() {}\n");
    }
    put(&root.join("app/Cargo.toml"), r#"[package]
name = "external-app"
version = "0.1.0"
edition = "2021"
[dependencies]
sensor_alias = { workspace = true, optional = true }
[target.'cfg(target_os = "none")'.dependencies]
imu_alias = { package = "nxrs-imu", path = "../imu" }
[features]
default = ["sensor"]
sensor = ["dep:sensor_alias"]
"#);
    put(&root.join("app/src/main.rs"), "fn main() {}\n");
    cargo(&["generate-lockfile", "--offline", "--manifest-path", "app/Cargo.toml"], root);
    let target = host_target(&mut Command::new("rustc")).unwrap();
    let manifest = root.join("app/Cargo.toml");
    let select = |options: &Options| CargoSelection::resolve(
        &manifest, options, Path::new(&target), Command::new("cargo")).unwrap();
    let ordinary = select(&Options::default());
    assert!(ordinary.contains("nxrs-detector"), "default feature must activate aliased workspace dependency");
    assert!(!ordinary.contains("nxrs-imu"), "inactive target must not activate IMU");
    let without_defaults = Options { no_default_features: true, ..Options::default() };
    assert!(!select(&without_defaults).contains("nxrs-detector"));
    let enabled = Options { features: vec!["sensor".into()], no_default_features: true,
                            ..Options::default() };
    assert!(select(&enabled).contains("nxrs-detector"));
    // Confirm the exact same feature combination is accepted by Cargo itself.
    cargo(&["check", "--locked", "--offline", "--manifest-path", "app/Cargo.toml",
           "--no-default-features", "--features", "sensor", "--target", &target], root);
}

#[test]
fn outside_workspace_application_consumes_camera_as_ordinary_cargo_dependency() {
    let temp = TempDir::new(&std::env::temp_dir()).unwrap();
    let root = &temp.0;
    let checkout = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
        .canonicalize().unwrap();
    put(&root.join("Cargo.toml"), &format!(r#"[package]
name = "external-camera-client"
version = "0.1.0"
edition = "2021"
[workspace]
[dependencies]
camera = {{package = "nxrs-camera", path = {:?}}}
"#, checkout.join("hal/camera")));
    put(&root.join("src/main.rs"), r#"fn main() {
    let _ = camera::open();
}
"#);
    cargo(&["generate-lockfile", "--offline"], root);
    cargo(&["check", "--locked", "--offline"], root);
    let selected = CargoSelection::resolve(
        &root.join("Cargo.toml"), &Options::default(),
        Path::new(&host_target(&mut Command::new("rustc")).unwrap()), Command::new("cargo")
    ).unwrap();
    assert!(selected.contains("nxrs-camera"));
}
