//! Independent linked ELF/capture fixtures exercise automatic companion loading.
#![cfg(target_os = "linux")]

use nxrs_memory_explorer::{
    discover_build_context, load_heap_journal, load_inputs, load_inputs_discovered,
    load_prepared_inputs, load_session,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

struct Fixture(PathBuf);

impl Fixture {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "nxrs-layout-discovery-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir(&path).unwrap();
        let output = Command::new("python3")
            .arg(Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/make_elf_fixture.py"))
            .arg(&path)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        Self(path)
    }

    fn path(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }

    fn identity(&self) -> String {
        format!(
            "{:x}",
            Sha256::digest(fs::read(self.path("fixture.elf")).unwrap())
        )
    }

    fn bind_layout(&self) {
        let path = self.path("layout.json");
        let mut value: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        value["build"] = json!({"elf_sha256":self.identity()});
        fs::write(path, serde_json::to_vec(&value).unwrap()).unwrap();
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn loose_elf_finds_named_linker_budgets_without_guessing_board_from_addresses() {
    let fixture = Fixture::new();
    let model =
        load_inputs_discovered(&fixture.path("fixture.elf"), None, &[], None, None).unwrap();
    assert!(model
        .regions
        .iter()
        .any(|region| region.name.starts_with("Linker FLASH")));
    assert!(model
        .regions
        .iter()
        .any(|region| region.name.starts_with("Linker RAM")));
    assert!(model.physical_resources.is_empty());
    assert!(model
        .warnings
        .iter()
        .any(|warning| warning.contains("no build.elf_sha256")));
    let exact = load_inputs(&fixture.path("fixture.elf"), None, &[], None).unwrap();
    assert!(!exact
        .regions
        .iter()
        .any(|region| region.name.starts_with("Linker ")));
}

#[test]
fn automatically_bound_layout_preserves_capture_and_journal_coordinates() {
    let fixture = Fixture::new();
    fixture.bind_layout();
    let capture: Value =
        serde_json::from_slice(&fs::read(fixture.path("exporter-snapshot.json")).unwrap()).unwrap();
    let id = capture["regions"][0]["id"].as_str().unwrap();
    let captured_start = capture["regions"][0]["start"].as_u64().unwrap();
    let captured_size = capture["regions"][0]["size"].as_u64().unwrap();
    let model = load_inputs_discovered(
        &fixture.path("fixture.elf"),
        None,
        &[fixture.path("exporter-snapshot.json")],
        None,
        None,
    )
    .unwrap();
    let region = model.regions.iter().find(|region| region.id == id).unwrap();
    assert_eq!((region.start, region.size), (captured_start, captured_size));
    assert!(model.snapshots[0]
        .spans
        .iter()
        .all(|span| span.region == id));
    assert!(!model.physical_resources.is_empty());
    let journal_path = fixture.path("heap-journal.json");
    let mut journal: Value = serde_json::from_slice(&fs::read(&journal_path).unwrap()).unwrap();
    journal["heap"]["region"] = json!(id);
    fs::write(&journal_path, serde_json::to_vec(&journal).unwrap()).unwrap();
    load_heap_journal(&journal_path, &model, None).unwrap();
    // A portable session passes only its inventoried companions and retains the
    // automatic overlay mode without rediscovering any neighboring file.
    let reopened = load_prepared_inputs(
        &fixture.path("fixture.elf"),
        Some(&fixture.path("fixture.map")),
        &[fixture.path("exporter-snapshot.json")],
        Some(&fixture.path("layout.json")),
        true,
        true,
    )
    .unwrap();
    load_heap_journal(&journal_path, &reopened, None).unwrap();
    assert_eq!(
        reopened.snapshots[0].spans[0].region,
        model.snapshots[0].spans[0].region
    );
}

#[test]
fn conflicting_automatic_backing_keeps_the_capture_and_explicit_identity_is_enforced() {
    let fixture = Fixture::new();
    fixture.bind_layout();
    let path = fixture.path("exporter-snapshot.json");
    let mut capture: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    let size = capture["regions"][0]["size"].as_u64().unwrap();
    capture["regions"][0]["physical"] = json!({"id":"captured-bank","offset":0});
    capture["physical_resources"] =
        json!([{"id":"captured-bank","name":"Captured bank","size":size}]);
    fs::write(&path, serde_json::to_vec(&capture).unwrap()).unwrap();
    let model =
        load_inputs_discovered(&fixture.path("fixture.elf"), None, &[path], None, None).unwrap();
    assert_eq!(model.physical_resources.len(), 1);
    assert_eq!(model.physical_resources[0].id, "captured-bank");
    assert!(model
        .warnings
        .iter()
        .any(|warning| warning.contains("conflicts with captured")));
    let layout_path = fixture.path("layout.json");
    let mut layout: Value = serde_json::from_slice(&fs::read(&layout_path).unwrap()).unwrap();
    layout["build"] = json!({"elf_sha256":"f".repeat(64)});
    fs::write(&layout_path, serde_json::to_vec(&layout).unwrap()).unwrap();
    assert!(
        load_inputs(&fixture.path("fixture.elf"), None, &[], Some(&layout_path))
            .unwrap_err()
            .to_string()
            .contains("different ELF")
    );
    assert!(load_inputs_discovered(
        &fixture.path("fixture.elf"),
        None,
        &[],
        Some(&layout_path),
        Some("mps2-an521-qemu")
    )
    .unwrap_err()
    .to_string()
    .contains("either"));
}

#[test]
fn build_context_follows_relocated_hash_receipts_and_rejects_changed_configuration() {
    let fixture = Fixture::new();
    let config = "CONFIG_ARCH_BOARD_MPS2_AN521=y\nCONFIG_ARCH_CHIP_MPS2_AN521=y\n";
    fs::write(fixture.path("resolved.config"), config).unwrap();
    fs::write(
        fixture.path("provenance.txt"),
        "board=mps2-an521:nsh\nnuttx=4268617f28ad0ed7e900a081ea1b33c9347458b3\n",
    )
    .unwrap();
    fs::write(
        fixture.path("binaries.sha256"),
        format!(
            "{}  /old/build/fixture.elf\n{:x}  /old/build/resolved.config\n",
            fixture.identity(),
            Sha256::digest(config.as_bytes())
        ),
    )
    .unwrap();
    let context = discover_build_context(&fixture.path("fixture.elf"))
        .unwrap()
        .unwrap();
    assert_eq!(context.board, "mps2-an521:nsh");
    assert_eq!(context.elf_sha256, fixture.identity());
    fs::write(
        fixture.path("resolved.config"),
        "CONFIG_ARCH_BOARD_MPS2_AN521=n\n",
    )
    .unwrap();
    assert!(discover_build_context(&fixture.path("fixture.elf"))
        .unwrap_err()
        .to_string()
        .contains("does not match"));
    let model =
        load_inputs_discovered(&fixture.path("fixture.elf"), None, &[], None, None).unwrap();
    assert!(model.physical_resources.is_empty());
    assert!(model
        .warnings
        .iter()
        .any(|warning| warning.contains("does not match")));
}

#[test]
fn packaged_automatic_layout_reopens_with_the_original_heap_journal_identity() {
    let fixture = Fixture::new();
    fixture.bind_layout();
    let capture: Value =
        serde_json::from_slice(&fs::read(fixture.path("exporter-snapshot.json")).unwrap()).unwrap();
    let id = capture["regions"][0]["id"].as_str().unwrap();
    let journal_path = fixture.path("heap-journal.json");
    let mut journal: Value = serde_json::from_slice(&fs::read(&journal_path).unwrap()).unwrap();
    journal["heap"]["region"] = json!(id);
    fs::write(&journal_path, serde_json::to_vec(&journal).unwrap()).unwrap();
    let console = fixture.path("console.txt");
    fs::write(
        &console,
        "worker-7 [0] 1.000000010: tracing_mark_write: I|7|imu/fixture/wake\n",
    )
    .unwrap();
    let output = Command::new("python3")
        .arg(Path::new(env!("CARGO_MANIFEST_DIR")).join("prepare_capture.py"))
        .arg("--elf")
        .arg(fixture.path("fixture.elf"))
        .arg("--console")
        .arg(console)
        .arg("--snapshot")
        .arg(fixture.path("exporter-snapshot.json"))
        .arg("--heap-journal")
        .arg(&journal_path)
        .arg("--output")
        .arg(fixture.path("portable"))
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let session = load_session(&fixture.path("portable")).unwrap();
    assert!(session.automatic_layout);
    assert!(session.automatic_map);
    let model = load_prepared_inputs(
        &session.elf,
        session.map.as_deref(),
        &session.snapshots,
        session.layout.as_deref(),
        session.automatic_map,
        session.automatic_layout,
    )
    .unwrap();
    assert_eq!(model.snapshots[0].spans[0].region, id);
    load_heap_journal(session.heap_journal.as_ref().unwrap(), &model, None).unwrap();
}

#[test]
fn bad_automatic_maps_are_optional_but_explicit_maps_fail_closed() {
    let fixture = Fixture::new();
    let elf = fixture.path("fixture.elf");
    let map = fixture.path("fixture.map");
    let original = fs::read_to_string(&map).unwrap();
    for wrong in [
        "not a GNU map".to_owned(),
        original.replace("0x0000000008000000", "0x0000000007000000"),
    ] {
        fs::write(&map, wrong).unwrap();
        let model = load_inputs_discovered(&elf, None, &[], None, None).unwrap();
        assert!(!model
            .regions
            .iter()
            .any(|region| region.name.starts_with("Linker ")));
        assert!(model
            .warnings
            .iter()
            .any(|warning| warning.contains("linker map was not applied")));
        assert!(load_inputs_discovered(&elf, Some(&map), &[], None, None).is_err());
        let reopened = load_prepared_inputs(&elf, Some(&map), &[], None, true, false).unwrap();
        assert!(!reopened
            .regions
            .iter()
            .any(|region| region.name.starts_with("Linker ")));
    }
    let file = fs::File::create(&map).unwrap();
    file.set_len(64 * 1024 * 1024 + 1).unwrap();
    let model = load_inputs_discovered(&elf, None, &[], None, None).unwrap();
    assert!(model
        .warnings
        .iter()
        .any(|warning| warning.contains("Skipped automatic linker map")));
    assert!(load_inputs_discovered(&elf, Some(&map), &[], None, None).is_err());
    fs::remove_file(&map).unwrap();
    std::os::unix::fs::symlink(&elf, &map).unwrap();
    let model = load_inputs_discovered(&elf, None, &[], None, None).unwrap();
    assert!(model
        .warnings
        .iter()
        .any(|warning| warning.contains("Skipped automatic linker map")));
    assert!(load_inputs_discovered(&elf, Some(&map), &[], None, None).is_err());
}
