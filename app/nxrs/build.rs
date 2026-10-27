use std::{env, path::PathBuf, process::Command};
fn main() {
    println!("cargo:rerun-if-env-changed=NXRS_PLATFORM");
    println!("cargo:rerun-if-env-changed=NXRS_PYTHON");
    let directory = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let root = directory.parent().unwrap().parent().unwrap();
    let configured = if let Some(profile) = env::var_os("NXRS_PLATFORM") {
        let code = r#"
import sys
sys.path.insert(0,sys.argv[1]+'/tools')
from platform_config import resolve,validate_target
profile=resolve(sys.argv[2])
validate_target(profile,sys.argv[3])
for path in profile['sources']: print('cargo:rerun-if-changed='+path)
print('ready' if all(profile['hal'].get(cap,'none') not in ('none','mock') for cap in ('camera','storage','transport')) else 'unbound')
"#;
        let result = Command::new(env::var_os("NXRS_PYTHON").unwrap_or_else(|| "python3".into()))
            .arg("-c")
            .arg(code)
            .arg(root)
            .arg(profile)
            .arg(env::var_os("CARGO_CFG_TARGET_OS").unwrap())
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        let output = String::from_utf8(result.stdout).unwrap();
        for line in output.lines().filter(|line| line.starts_with("cargo:")) {
            println!("{line}");
        }
        output.lines().last() == Some("ready")
    } else {
        false
    };
    println!(
        "cargo:rerun-if-changed={}",
        root.join("tools/platform_config.py").display()
    );
    let source = if configured {
        format!(
            "#[path = {:?}] mod cli;\npub fn run()->std::process::ExitCode {{ cli::main() }}\n",
            directory.join("src/cli.rs")
        )
    } else {
        "pub fn run()->std::process::ExitCode { eprintln!(\"nxrs requires a profile binding camera, storage and transport; set NXRS_PLATFORM\"); std::process::ExitCode::FAILURE }\n".into()
    };
    std::fs::write(
        PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("cli_entry.rs"),
        source,
    )
    .unwrap();
}
