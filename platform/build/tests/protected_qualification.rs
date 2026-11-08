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
fn shared_runtime_access_requires_qualification_without_exposing_provider_internals() {
    let repo = Repo::new();
    let fixture = TempDir::new(&env::temp_dir()).unwrap();
    let app = fixture.0.join("Cargo.toml");
    for (qualification_only, profile, dependency, features, expected_error) in [
        (
            true,
            "cores3-imu-qualification",
            "hal/acquisition",
            "",
            None,
        ),
        (
            false,
            "cores3-imu-qualification",
            "hal/acquisition",
            "",
            Some("concrete HAL implementation"),
        ),
        (
            true,
            "m5stack-cores3-gnss",
            "hal/acquisition",
            "",
            Some("concrete HAL implementation"),
        ),
        (
            true,
            "cores3-imu-qualification",
            "hal/acquisition/src/platform/nuttx",
            "",
            Some("concrete HAL implementation"),
        ),
        (
            true,
            "cores3-imu-qualification",
            "hal/imu/src/platform/nuttx",
            "",
            Some("concrete HAL implementation"),
        ),
        (
            true,
            "cores3-imu-qualification",
            "hal/acquisition",
            "'nuttx'",
            Some("provider feature"),
        ),
    ] {
        fs::write(
            &app,
            format!(
                "[package]\nname = 'runtime-fixture'\nversion = '0.1.0'\n\
             [package.metadata.nxrs]\nqualification-only = {qualification_only}\n\
             [dependencies.renamed]\npath = {:?}\nfeatures = [{features}]\n",
                repo.root.join(dependency).to_str().unwrap(),
            ),
        )
        .unwrap();
        let result = repo.validate(&app, &repo.profile(profile));
        match expected_error {
            Some(expected) => {
                let error = result.unwrap_err();
                assert!(error.contains(expected), "{dependency}: {error}");
            }
            None => {
                result.unwrap();
            }
        }
    }
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

#[test]
fn devkit_uses_the_same_bus_in_both_modes_and_supports_periodic_controls() {
    let repo = Repo::new();
    let flat = repo.profile("esp32s3-devkit-imu-qualification");
    let protected = repo.profile("esp32s3-devkit-imu-protected-qualification");
    assert_eq!(flat.data["kconfig"], protected.data["kconfig"]);
    assert_eq!(flat.data["build"]["size-optimized"], true);
    assert_eq!(protected.data["build"]["size-optimized"], true);
    let flat_graph = flat.composition().unwrap().unwrap();
    let protected_graph = protected.composition().unwrap().unwrap();
    let binding = flat_graph.imu().unwrap();
    assert_eq!(Some(binding), protected_graph.imu());
    assert_eq!(binding.path, "/dev/i2c0");
    assert_eq!(binding.irq_path, Some("/dev/gpio2"));

    for profile in [
        "esp32s3-devkit-imu-qualification",
        "esp32s3-devkit-imu-protected-qualification",
    ] {
        let periodic = Profile::resolve_with_overlays(
            &repo.root,
            profile,
            &[PathBuf::from(
                "tests/nuttx-std/imu-qualification/overlays/bmi270-periodic.toml",
            )],
        )
        .unwrap();
        let graph = periodic.composition().unwrap().unwrap();
        let periodic = graph.imu().unwrap();
        assert_eq!(periodic.path, binding.path);
        assert_eq!(periodic.address, binding.address);
        assert_eq!(periodic.frequency, binding.frequency);
        assert_eq!(periodic.implementation, binding.implementation);
        assert_eq!(periodic.irq_path, None);
    }

    let lsm = Profile::resolve_with_overlays(
        &repo.root,
        "esp32s3-devkit-imu-protected-qualification",
        &[PathBuf::from(
            "tests/nuttx-std/imu-qualification/overlays/lsm6dso.toml",
        )],
    )
    .unwrap();
    let graph = lsm.composition().unwrap().unwrap();
    let binding = graph.imu().unwrap();
    assert_eq!(binding.path, "/dev/i2c0");
    assert_eq!(binding.address, 0x6a);
    assert_eq!(binding.irq_path, None);
}

#[test]
fn sensor_reference_and_delivery_marker_are_explicit_in_both_modes() {
    let repo = Repo::new();
    let app = repo.app("tests/nuttx-std/imu-qualification/Cargo.toml");
    for (base, reference, mode) in [
        (
            "esp32s3-devkit-imu-qualification",
            "esp32s3-devkit-imu-sensor-reference",
            "flat",
        ),
        (
            "esp32s3-devkit-imu-protected-qualification",
            "esp32s3-devkit-imu-protected-sensor-reference",
            "protected",
        ),
    ] {
        let base = repo.profile(base);
        let reference = repo.profile(reference);
        assert!(base.data["init"]["args"].is_null());
        assert_eq!(
            reference.data["init"]["args"],
            json!(["--sensor-reference", "--delivery-marker"])
        );
        assert_eq!(base.data["kconfig"], reference.data["kconfig"]);
        assert_eq!(
            base.composition().unwrap().unwrap().imu(),
            reference.composition().unwrap().unwrap().imu()
        );
        assert_eq!(
            repo.validate(&app, &reference).unwrap()["nuttx-build-mode"],
            mode
        );
    }
}
