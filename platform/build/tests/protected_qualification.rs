//! Admission boundaries for the CoreS3 protected-runtime qualification build.
use nxrs_build::{deployment, profile::Profile, TempDir};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::{env, fs};

struct Repo {
    root: PathBuf,
}

impl Repo {
    fn new() -> Self {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .canonicalize()
            .unwrap();
        Self { root }
    }

    fn profile(&self, name: &str) -> Profile {
        Profile::resolve(&self.root, name).unwrap()
    }

    fn app(&self, path: &str) -> PathBuf {
        self.root.join(path)
    }

    fn validate(&self, app: &Path, profile: &Profile) -> Result<Value, String> {
        deployment::validate(&self.root, app, profile, "nuttx")
    }
}

#[test]
fn ordinary_app_stays_flat_and_cannot_select_protected_qualification() {
    let repo = Repo::new();
    let profile = repo.profile("m5stack-cores3-gnss");
    let fixture = TempDir::new(&env::temp_dir()).unwrap();
    let app = fixture.0.join("Cargo.toml");
    fs::write(
        &app,
        "[package]\nname = \"ordinary-sensor-fixture\"\nversion = \"0.1.0\"\n",
    )
    .unwrap();

    let evidence = repo.validate(&app, &profile).unwrap();
    assert_eq!(evidence["nuttx-build-mode"], "flat");
    assert_eq!(profile.data["board"], "esp32s3-m5-cores3:nsh");

    let protected = repo.profile("cores3-protected-qualification");
    let error = repo.validate(&app, &protected).unwrap_err();
    assert!(error.contains("qualification-only = true"), "{error}");
}

#[test]
fn qualification_profiles_keep_the_same_sensor_bindings_and_select_mode() {
    let repo = Repo::new();
    let flat = repo.profile("cores3-imu-qualification");
    let protected = repo.profile("cores3-protected-qualification");
    let app = repo.app("tests/nuttx-std/imu-protected/runtime/Cargo.toml");

    let flat_evidence = repo.validate(&app, &flat).unwrap();
    let protected_evidence = repo.validate(&app, &protected).unwrap();
    assert_eq!(flat_evidence["nuttx-build-mode"], "flat");
    assert_eq!(protected_evidence["nuttx-build-mode"], "protected");

    let flat_graph = flat.composition().unwrap().unwrap();
    let protected_graph = protected.composition().unwrap().unwrap();
    assert_eq!(flat_graph.imu(), protected_graph.imu());
    assert_eq!(flat_graph.gnss(), protected_graph.gnss());
    assert_eq!(protected_graph.imu().unwrap().path, "/dev/i2c0");
    assert_eq!(protected_graph.imu().unwrap().address, 0x68);
}

#[test]
fn protected_runtime_requires_unsigned_direct_init_qualification() {
    let repo = Repo::new();
    let app = repo.app("tests/nuttx-std/imu-protected/runtime/Cargo.toml");
    let protected = repo.profile("cores3-protected-qualification");

    let mut without_init = protected.clone();
    without_init.data.as_object_mut().unwrap().remove("init");
    let error = repo.validate(&app, &without_init).unwrap_err();
    assert!(
        error.contains("unsigned, direct-init ESP32-S3 qualification"),
        "{error}"
    );

    let mut signed = protected;
    signed
        .data
        .as_object_mut()
        .unwrap()
        .insert("mcuboot".into(), json!({}));
    let error = repo.validate(&app, &signed).unwrap_err();
    assert!(
        error.contains("unsigned, direct-init ESP32-S3 qualification"),
        "{error}"
    );
}
