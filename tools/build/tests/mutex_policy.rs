//! Public selection/edit paths must enforce the policy before native preparation.
use nxrs_build::{
    build::{App, Options},
    config_edit::{Draft, Session},
    nuttx::Inspection,
    profile::Profile,
    TempDir,
};
use serde_json::Value;
use std::{
    collections::BTreeMap,
    env, fs,
    path::Path,
    process::{Command, Output},
};

fn write(root: &Path, path: &str, text: &str) {
    let path = root.join(path);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, text).unwrap();
}

fn fixture(qualification: bool) -> TempDir {
    let temp = TempDir::new(&env::temp_dir()).unwrap();
    write(&temp.0, "Cargo.toml", "[workspace]\nresolver='2'\n");
    fs::create_dir(temp.0.join("hal")).unwrap();
    write(&temp.0, "app/probe/Cargo.toml", &format!(
        "[package]\nname='probe'\nversion='0.1.0'\n[package.metadata.nxrs]\nqualification-only={qualification}\n[workspace]\n"
    ));
    write(&temp.0, "app/probe/src/main.rs", "fn main() {}\n");
    write(
        &temp.0,
        "tests/profiles/board.toml",
        "board='fixture:nsh'\n[hal]\n",
    );
    write(
        &temp.0,
        "tests/profiles/host.toml",
        "execution='std'\n[hal]\n",
    );
    temp
}

fn frontend(root: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_nxrs-build"))
        .current_dir(root)
        .arg("--workspace-root")
        .arg(root)
        .args(args)
        .output()
        .unwrap()
}

fn failure(output: Output, text: &str) {
    assert!(!output.status.success(), "unexpected success");
    let error = String::from_utf8_lossy(&output.stderr);
    assert!(error.contains(text), "{error}");
}

#[test]
fn ordinary_and_detached_entries_receive_default_policy() {
    let root = fixture(false);
    let detached = TempDir::new(&env::temp_dir()).unwrap();
    write(
        &detached.0,
        "Cargo.toml",
        "[package]\nname='detached'\nversion='0.1.0'\n[workspace]\n",
    );
    write(&detached.0, "src/main.rs", "fn main() {}\n");
    for entry in [
        root.0.join("app/probe/Cargo.toml"),
        detached.0.join("Cargo.toml"),
    ] {
        let output = frontend(
            &root.0,
            &[
                "build",
                "--manifest-path",
                entry.to_str().unwrap(),
                "--profile",
                "board",
                "--dry-run",
            ],
        );
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let report: Value = serde_json::from_slice(&output.stdout).unwrap();
        let policy = &report["mutex_policy"];
        assert_eq!(policy["mode"], "inherit");
        assert_eq!(policy["holder_tracking"], "embedded-single-holder");
        assert_eq!(policy["kconfig"]["SEM_PREALLOCHOLDERS"], "0");
        assert_eq!(policy, &report["deployment"]["mutex_policy"]);
        assert!(!root.0.join("target").exists());
    }
}

#[test]
fn all_frontend_operations_reject_conflicting_native_requests_before_sdk_access() {
    let root = fixture(false);
    for operation in [
        vec!["build", "--dry-run"],
        vec!["config"],
        vec!["config", "check"],
        vec!["config", "show"],
        vec!["config", "explain", "CONFIG_PRIORITY_INHERITANCE"],
    ] {
        for request in [
            ["--kconfig-disable", "PRIORITY_INHERITANCE"],
            ["--kconfig-enable", "PTHREAD_MUTEX_DEFAULT_PRIO_NONE"],
            ["--kconfig-disable", "PTHREAD_MUTEX_DEFAULT_PRIO_INHERIT"],
            ["--kconfig-set", "SEM_PREALLOCHOLDERS=8"],
        ] {
            let mut args = operation.clone();
            args.extend(["--app", "probe", "--profile", "board"]);
            args.extend(request);
            failure(frontend(&root.0, &args), "NXRS resource-mutex policy");
        }
    }
    assert!(!root.0.join("target").exists());
}

#[test]
fn profile_defaults_fragments_and_overlays_cannot_disable_policy() {
    let root = fixture(false);
    for config in [
        "[kconfig]\ndisable=['PRIORITY_INHERITANCE']\n",
        "[kconfig]\nrequire=['# CONFIG_PTHREAD_MUTEX_DEFAULT_PRIO_INHERIT is not set']\n",
        "[kconfig]\nforbid-regex='^CONFIG_PRIORITY_INHERITANCE=y$'\n",
        "nuttx-config='native.conf'\n",
    ] {
        write(
            &root.0,
            "tests/profiles/board.toml",
            &format!("board='fixture:nsh'\n{config}"),
        );
        write(
            &root.0,
            "tests/profiles/native.conf",
            "# CONFIG_PRIORITY_INHERITANCE is not set\n",
        );
        failure(
            frontend(
                &root.0,
                &["build", "--app", "probe", "--profile", "board", "--dry-run"],
            ),
            "NXRS resource-mutex policy",
        );
    }
    write(
        &root.0,
        "tests/profiles/board.toml",
        "board='fixture:nsh'\n[hal]\n",
    );
    write(&root.0, "override.conf", "CONFIG_SEM_PREALLOCHOLDERS=32\n");
    failure(
        frontend(
            &root.0,
            &[
                "build",
                "--app",
                "probe",
                "--profile",
                "board",
                "--overlay",
                "override.conf",
                "--dry-run",
            ],
        ),
        "NXRS resource-mutex policy",
    );
}

#[test]
fn qualification_controls_are_explicit_explained_and_unavailable_to_products_or_hosts() {
    let product = fixture(false);
    let fixture = fixture(true);
    let selection = ["build", "--app", "probe", "--profile", "board", "--dry-run"];
    for mode in ["no-inherit", "kernel-pi-disabled"] {
        let mut args = selection.to_vec();
        args.extend(["--qualification-mutex-policy", mode]);
        failure(frontend(&fixture.0, &args), "--qualification-mutex-reason");
        args.extend(["--qualification-mutex-reason", "retained negative control"]);
        failure(frontend(&product.0, &args), "qualification-only");
        let output = frontend(&fixture.0, &args);
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let report: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(report["mutex_policy"]["mode"], mode);
        assert_eq!(
            report["mutex_policy"]["reason"],
            "retained negative control"
        );
        assert_eq!(report["mutex_policy"]["source"], "qualification-control");
        let board = args.iter().position(|s| *s == "board").unwrap();
        args[board] = "host";
        failure(frontend(&fixture.0, &args), "NuttX profile");
    }
    // Eligibility is not an exemption: a fixture still defaults to inherit.
    let output = frontend(&fixture.0, &selection);
    assert!(output.status.success());
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["mutex_policy"]["mode"], "inherit");
}

#[test]
fn editor_preview_cannot_publish_a_conflicting_policy_draft() {
    let root = fixture(false);
    let app = App::load(&root.0.join("app/probe/Cargo.toml"), None).unwrap();
    let profile = Profile::resolve(&root.0, "board").unwrap();
    let session = Session::new(
        root.0.clone(),
        app,
        profile,
        root.0.join("target/config"),
        Options::default(),
    )
    .unwrap();
    let source = fs::read(root.0.join("tests/profiles/board.toml")).unwrap();
    let draft = Draft {
        symbols: BTreeMap::from([("PRIORITY_INHERITANCE".into(), Some("n".into()))]),
        ..Draft::default()
    };
    let preview = session.preview(&draft, &mut Inspection::new()).unwrap();
    assert!(preview.report["problems"]
        .to_string()
        .contains("NXRS resource-mutex policy"));
    assert!(session.publish(&preview).is_err());
    assert_eq!(
        fs::read(root.0.join("tests/profiles/board.toml")).unwrap(),
        source
    );
    assert!(!root.0.join("tests/profiles/board.conf").exists());
    assert!(!root.0.join("target").exists());
}
