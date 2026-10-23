//! Build-only product composition; no model selection enters a service.
use std::{env, path::Path, process::Command};

pub struct Config {
    pub model: String,
    #[allow(dead_code)]
    pub stack_bytes: usize,
    #[allow(dead_code)]
    pub sample_ms: u64,
}

pub fn configure(root: &Path) -> Option<Config> {
    println!("cargo:rerun-if-env-changed=NXRS_PLATFORM");
    println!("cargo:rerun-if-env-changed=NXRS_PYTHON");
    let platform = env::var_os("NXRS_PLATFORM")?;
    let resolver = root.join("tools/platform_config.py");
    println!("cargo:rerun-if-changed={}", resolver.display());
    let output = Command::new(env::var_os("NXRS_PYTHON").unwrap_or_else(|| "python3".into()))
        .arg(resolver)
        .arg("--platform")
        .arg(platform)
        .arg("--detector-config")
        .output()
        .expect("Product composition requires Python 3.11+");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let output = String::from_utf8(output.stdout).unwrap();
    let values: Vec<_> = output
        .lines()
        .filter(|line| {
            if line.starts_with("cargo:") {
                println!("{line}");
                false
            } else {
                true
            }
        })
        .collect();
    if values.is_empty() {
        return None;
    }
    assert_eq!(values.len(), 3, "Invalid detector product configuration");
    Some(Config {
        model: values[0].into(),
        stack_bytes: values[1].parse().unwrap(),
        sample_ms: values[2].parse().unwrap(),
    })
}
