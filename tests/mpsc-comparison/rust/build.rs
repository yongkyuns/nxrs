fn main() {
    println!("cargo:rerun-if-env-changed=MPSC_BUILD_ID");
    println!("cargo:rerun-if-env-changed=MPSC_CLOCK_HZ");
    println!("cargo:rerun-if-env-changed=MPSC_BACKEND");
    println!("cargo:rerun-if-env-changed=MPSC_OBSERVER");
    let hz = clock();
    assert!(matches!(hz, 1000000 | 240000000));
    let out = std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
    let target = std::env::var("CARGO_CFG_TARGET_OS").expect("target OS");
    let observer = std::env::var("MPSC_OBSERVER").unwrap_or_else(|_| "primary".into());
    assert!(
        observer == "primary"
            || matches!(observer.as_str(), "cpu" | "irq")
                && hz == 240000000
                && matches!(target.as_str(), "nuttx" | "none"),
        "unsupported MPSC observer for target"
    );
    let cpu_scope = match (observer.as_str(), target.as_str()) {
        ("cpu", "nuttx") => "nuttx-thread-runtime-includes-attributed-irqs",
        ("cpu", "none") => "zephyr-thread-runtime-excludes-isr-dispatch",
        _ => "unavailable",
    };
    println!("cargo:rustc-env=MPSC_OBSERVER={observer}");
    println!("cargo:rustc-env=MPSC_CPU_SCOPE={cpu_scope}");
    println!("cargo:rustc-check-cfg=cfg(mpsc_irq_observer)");
    if observer == "irq" {
        println!("cargo:rustc-cfg=mpsc_irq_observer");
    }
    let backend = std::env::var("MPSC_BACKEND")
        .unwrap_or_else(|_| if target == "none" { "native" } else { "std" }.into());
    assert!(
        matches!(
            (target.as_str(), backend.as_str()),
            ("nuttx", "std" | "native") | ("none", "native")
        ) || target != "nuttx" && target != "none" && backend == "std",
        "unsupported MPSC backend for target"
    );
    // Select one entry at build time. The unused transport is not reachable
    // from the firmware entry, so deployment images do not link both engines.
    std::fs::write(
        out.join("backend.rs"),
        format!("pub fn execute(words: &[&str]) {{ crate::execute_{backend}(words); }}\n"),
    )
    .unwrap();
    std::fs::write(
        out.join("clock.rs"),
        format!("pub const CLOCK_HZ: u32 = {hz};"),
    )
    .unwrap();
    let id = std::env::var("MPSC_BUILD_ID").unwrap_or_else(|_| "host".into());
    assert!(id == "host" || id.len() == 64 && id.bytes().all(|b| b.is_ascii_hexdigit()));
    println!("cargo:rustc-env=MPSC_BUILD_ID={id}");
}

fn clock() -> u32 {
    std::env::var("MPSC_CLOCK_HZ")
        .map(|s| s.parse().expect("clock integer"))
        .unwrap_or(240000000)
}
