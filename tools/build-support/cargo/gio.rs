//! Own the GIO ABI once. Contract-only and mock builds need no GLib SDK.
use nxrs_build_support::{native, selected_profile, Result};
use std::{env, path::PathBuf};

fn main() -> Result<()> {
    let here = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").ok_or("missing manifest")?);
    let Some((root, profile)) = selected_profile(&here)? else {
        return Ok(());
    };
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
