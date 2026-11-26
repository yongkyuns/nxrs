//! An independently linked ELF and the Python exporter's real JSON serializer
//! exercise the import boundary. Target objects are synthetic, not device proof.
#![cfg(target_os = "linux")]

use nxrs_memory_explorer::{load_inputs, AddressDomain, SpanKind, TimeConfidence};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

struct Fixture(PathBuf);

impl Fixture {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "nxrs-memory-elf-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir(&path).unwrap();
        let fixture = Self(path);
        let output = Command::new("python3")
            .arg(Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/make_elf_fixture.py"))
            .arg(&fixture.0)
            .output()
            .expect("Linux integration tests require python3, cc and GNU ld");
        assert!(
            output.status.success(),
            "fixture linker/exporter failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        fixture
    }

    fn path(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        // This uniquely owned directory contains only this test's generated inputs.
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn linked_elf_and_python_capture_preserve_addresses_provenance_and_unknowns() {
    let fixture = Fixture::new();
    let elf = fixture.path("fixture.elf");
    let map = fixture.path("fixture.map");
    let layout = fixture.path("layout.json");
    let snapshot = fixture.path("exporter-snapshot.json");
    let model = load_inputs(&elf, Some(&map), &[], Some(&layout)).unwrap();
    let expected: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(fixture.path("expected.json")).unwrap()).unwrap();
    assert_eq!(model.build.elf_sha256, expected["elf_sha256"]);
    let ram = model
        .regions
        .iter()
        .find(|region| region.id == "fixture-ram-window")
        .unwrap();
    assert_eq!(ram.start, 0x2000_0000);
    assert_eq!(ram.size, 16384);
    let section = |name: &str| {
        model
            .static_spans
            .iter()
            .find(|span| span.name == name && span.region == ram.id && span.parent.is_none())
            .unwrap()
    };
    assert_eq!(section(".data").start, 0x2000_0000);
    assert_eq!(section(".data").size, 4);
    assert_eq!(section(".bss").size, 128);
    assert_eq!(section(".bss").kind, SpanKind::Bss);
    assert_eq!(section(".heap").start, 0x2000_1000);
    assert_eq!(section(".heap").size, 4096);
    assert_eq!(section(".heap").kind, SpanKind::Reserved);
    assert!(model
        .static_spans
        .iter()
        .any(|span| { span.name == ".data" && span.region == "elf-file" && span.size == 4 }));
    assert!(!model.static_spans.iter().any(|span| {
        span.region == "elf-file" && (span.name == ".bss" || span.name == ".heap")
    }));
    assert!(model.symbols.iter().any(|symbol| {
        symbol.name == "initialized" && symbol.start == 0x2000_0000 && symbol.size == 4
    }));
    assert!(model.static_spans.iter().any(|span| {
        span.region == ram.id
            && span.start == 0x2000_0000
            && span.source.as_deref() == Some("application.o")
    }));
    assert_eq!(model.free_bytes(&ram.id, 0), 0);
    assert_eq!(
        model.view(&ram.id, ram.range().unwrap(), 0).runtime,
        TimeConfidence::Unknown
    );

    let model = load_inputs(
        &elf,
        Some(&map),
        std::slice::from_ref(&snapshot),
        Some(&layout),
    )
    .unwrap();
    let ram = model
        .regions
        .iter()
        .find(|region| region.id == "fixture-ram-window")
        .unwrap();
    let captured = &model.snapshots[0];
    assert_eq!(captured.time_ns, 0);
    assert!(captured.spans.iter().all(|span| span.region == ram.id));
    let allocation = captured
        .spans
        .iter()
        .find(|span| span.kind == SpanKind::Allocation)
        .unwrap();
    assert_eq!(allocation.size, 1024);
    let stack = captured
        .spans
        .iter()
        .find(|span| span.kind == SpanKind::Stack)
        .unwrap();
    assert_eq!(stack.size, 512);
    assert_eq!(stack.parent.as_deref(), Some(allocation.id.as_str()));
    assert!(allocation.range().unwrap().contains(stack.range().unwrap()));
    let header = allocation.start - 0x2000_1000;
    assert!(header == 8 || header == 16);
    assert_eq!(model.free_bytes(&ram.id, 0), 4096 - 1024 - 2 * header);
    // Static .data/.bss, two sentinels, and one allocated chunk. The stack is
    // nested inside that chunk; the reserved ELF heap is replaced by its capture.
    assert_eq!(
        model.occupied_bytes(&ram.id, 0),
        4 + 128 + 2 * header + 1024
    );
    assert_eq!(
        model.physical_occupied_bytes(0)["fixture-sram"],
        model.occupied_bytes(&ram.id, 0)
    );
    assert_eq!(model.free_bytes(&ram.id, 1), 0);
    assert_eq!(
        model.view(&ram.id, ram.range().unwrap(), 1).runtime,
        TimeConfidence::Unknown
    );
    assert!(model
        .warnings
        .iter()
        .any(|warning| warning.contains("Synthetic exporter")));
    assert!(model
        .regions
        .iter()
        .any(|region| region.domain == AddressDomain::Image && region.physical.is_none()));

    let mut document: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(snapshot).unwrap()).unwrap();
    document["build"]["elf_sha256"] = serde_json::Value::String("f".repeat(64));
    let wrong_snapshot = fixture.path("wrong-build.json");
    fs::write(&wrong_snapshot, serde_json::to_vec(&document).unwrap()).unwrap();
    assert!(
        load_inputs(&elf, Some(&map), &[wrong_snapshot], Some(&layout))
            .unwrap_err()
            .to_string()
            .contains("SHA-256 mismatch")
    );
    let wrong_map = fixture.path("wrong-build.map");
    fs::write(&wrong_map, ".data 0x20000000 0x5\n").unwrap();
    assert!(load_inputs(&elf, Some(&wrong_map), &[], None)
        .unwrap_err()
        .to_string()
        .contains("does not match ELF"));
}
