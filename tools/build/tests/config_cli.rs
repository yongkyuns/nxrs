//! Public configuration discovery must work before any target SDK is installed.
use nxrs_build::TempDir;
use std::{
    env, fs,
    path::Path,
    process::{Command, Output},
};

fn frontend(root: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_nxrs-build"))
        .current_dir(root)
        .arg("--workspace-root")
        .arg(root)
        .args(args)
        .output()
        .unwrap()
}

fn failure(output: Output) -> String {
    assert!(!output.status.success(), "command unexpectedly succeeded");
    String::from_utf8(output.stderr).unwrap()
}

fn write(root: &Path, path: &str, contents: &str) {
    let path = root.join(path);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, contents).unwrap();
}

fn fixture() -> TempDir {
    let root = TempDir::new(&env::temp_dir()).unwrap();
    write(&root.0, "Cargo.toml", "[workspace]\nresolver = \"2\"\n");
    fs::create_dir(root.0.join("hal")).unwrap();
    for app in ["probe", "other"] {
        write(
            &root.0,
            &format!("app/{app}/Cargo.toml"),
            &format!(
                "[package]\nname = \"{app}\"\nversion = \"0.1.0\"\nedition = \"2021\"\n[workspace]\n"
            ),
        );
        write(&root.0, &format!("app/{app}/src/main.rs"), "fn main() {}\n");
    }
    write(
        &root.0,
        "tests/profiles/base.toml",
        "board = \"fixture:nsh\"\n[hal]\n",
    );
    write(
        &root.0,
        "platform/wroom/profile.toml",
        "name = \"inherited\"\nextends = \"base\"\n",
    );
    write(
        &root.0,
        "platform/direct/profile.toml",
        "name = \"direct\"\nboard = \"fixture:nsh\"\n[hal]\n",
    );
    write(
        &root.0,
        "platform/host/profile.toml",
        "name = \"host\"\nexecution = \"std\"\n[hal]\n",
    );
    root
}

#[test]
fn config_help_is_complete_before_workspace_discovery() {
    let outside = TempDir::new(&env::temp_dir()).unwrap();
    let cases: &[&[&str]] = &[
        &["config", "--help"],
        &["config", "-h"],
        &["config", "help"],
        &["config", "check", "--help"],
        &["config", "show", "-h"],
        &["config", "explain", "--help"],
        &["config", "explain", "CONFIG_DEBUG_ASSERTIONS", "--help"],
        &["config", "recover", "--help"],
        &["--workspace-root", "missing", "config", "--help"],
    ];
    for args in cases {
        let output = Command::new(env!("CARGO_BIN_EXE_nxrs-build"))
            .current_dir(&outside.0)
            .args(*args)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{args:?}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        let help = String::from_utf8(output.stdout).unwrap();
        for expected in [
            "--app <app>",
            "--manifest-path <path>",
            "--profile <profile>",
            "--overlay <path>",
            "cargo nxrs apps",
            "cargo nxrs platforms",
            "cargo nxrs config check --app APP --platform PLATFORM",
            "cargo nxrs config explain CONFIG_DEBUG_ASSERTIONS --app APP --platform PLATFORM",
            "cargo nxrs config recover",
            "kconfiglib==14.1.0",
            "target compiler/SDK",
        ] {
            assert!(help.contains(expected), "{args:?}: missing {expected:?}");
        }
    }
}

#[test]
fn config_platform_choices_include_inherited_nuttx_boards() {
    let root = fixture();
    let message = failure(frontend(&root.0, &["config", "check", "--app", "probe"]));
    assert!(
        message.contains("select --platform (direct, wroom)"),
        "{message}"
    );
    assert!(!message.contains("host"), "{message}");
}

#[test]
fn config_platform_discovery_reports_invalid_profiles() {
    let root = fixture();
    write(
        &root.0,
        "platform/broken/profile.toml",
        "name = \"broken\"\nextends = \"missing\"\n",
    );
    let message = failure(frontend(&root.0, &["config", "check", "--app", "probe"]));
    assert!(
        message.contains("cannot inspect platform broken"),
        "{message}"
    );
    assert!(
        message.contains("unknown or ambiguous platform: missing"),
        "{message}"
    );
    assert!(!message.contains("select --platform"), "{message}");
}

#[test]
fn missing_application_hint_names_the_accepted_flag() {
    let root = fixture();
    let message = failure(frontend(
        &root.0,
        &["config", "check", "--platform", "direct"],
    ));
    assert!(message.contains("select --app (other, probe)"), "{message}");
    assert!(!message.contains("--application"), "{message}");
    // Following the hint gets past application selection without preparing NuttX.
    let message = failure(frontend(&root.0, &["config", "check", "--app", "probe"]));
    assert!(message.contains("select --platform"), "{message}");
}
