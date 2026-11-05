//! Own the GIO ABI once. Contract-only and mock builds need no GLib SDK.
use nxrs_build_support::{native, profile::Profile, sdk_root, Result};
use std::{env, path::PathBuf};

fn main() -> Result<()> {
    println!("cargo:rerun-if-env-changed=NXRS_PLATFORM");
    println!("cargo:rerun-if-env-changed=NXRS_SDK_ROOT");
    let Some(name) = env::var_os("NXRS_PLATFORM") else {
        return Ok(());
    };
    let here = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").ok_or("missing manifest")?);
    let root = sdk_root(&here)?;
    let profile = Profile::resolve(&root, name.to_str().ok_or("platform name must be UTF-8")?)?;
    // Also watch provider declarations used by profile validation when disabled.
    for capability in ["wifi", "ble"] {
        for line in profile.cargo_output(
            &root,
            capability,
            &env::var("CARGO_CFG_TARGET_OS").map_err(|e| e.to_string())?,
        )? {
            if line.starts_with("cargo:rerun-if-changed=") {
                println!("{line}");
            }
        }
    }
    // A profile may bind capabilities that this application never consumes.
    // Cargo client features identify only dependency usage; the profile still
    // selects the implementation. An unbound WiFi consumer must not need GIO
    // merely because the product also declares an unused Linux BLE binding.
    if ![
        ("wifi", cfg!(feature = "wifi")),
        ("ble", cfg!(feature = "ble")),
    ]
    .iter()
    .any(|(name, consumed)| *consumed && profile.binding(name) == "linux")
    {
        return Ok(());
    }
    let out = PathBuf::from(env::var_os("OUT_DIR").ok_or("missing OUT_DIR")?);
    let target = env::var("TARGET").map_err(|e| e.to_string())?;
    for line in native::build(&here, "linux", &target, &out)? {
        println!("{line}");
    }
    Ok(())
}
