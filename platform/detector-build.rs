//! Build-only product composition; services consume the detector contract.
use std::{env, path::Path};
pub type Config = nxrs_build::profile::Detector;
pub fn configure(root: &Path) -> Option<Config> {
    println!("cargo:rerun-if-env-changed=NXRS_PLATFORM");
    let name = env::var("NXRS_PLATFORM").ok()?;
    let profile =
        nxrs_build::profile::Profile::resolve(root, &name).unwrap_or_else(|e| panic!("{e}"));
    for path in &profile.sources {
        println!("cargo:rerun-if-changed={}", path.display())
    }
    profile.detector().unwrap_or_else(|e| panic!("{e}"))
}
