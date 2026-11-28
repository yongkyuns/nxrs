use serde_json::Value;
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;
use std::process::Command;

fn json(path: &Path) -> Value {
    serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap()
}

pub fn source_pins() -> BTreeMap<&'static str, String> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let release = json(&root.join("upstream/nuttx/release.json"));
    let mut pins = BTreeMap::new();
    for (name, component) in [("nuttx", "nuttx"), ("apps", "nuttx-apps")] {
        let output = Command::new("git")
            .current_dir(&root)
            .args(["rev-parse", &format!(":external/{component}")])
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "missing checked-in {component} pin"
        );
        let revision = String::from_utf8(output.stdout).unwrap().trim().to_owned();
        assert!(revision.len() == 40 && revision.bytes().all(|b| b.is_ascii_hexdigit()));
        pins.insert(name, revision);
    }
    assert_eq!(release["schema"], 1);
    assert_eq!(release["revision"].as_str().unwrap(), pins["nuttx"]);
    pins
}
