//! An independently linked ELF and the Python exporter's real JSON serializer
//! exercise the import boundary. Target objects are synthetic, not device proof.
#![cfg(target_os = "linux")]

use nxrs_memory_explorer::{
    load_heap_journal, load_inputs, AddressDomain, SpanKind, TimeConfidence,
};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

struct Fixture(PathBuf);

#[test]
fn linker_memory_budgets_give_named_navigation_without_inventing_board_backing() {
    let fixture = Fixture::new();
    let model = load_inputs(
        &fixture.path("fixture.elf"),
        Some(&fixture.path("fixture.map")),
        &[],
        None,
    )
    .unwrap();
    let flash = model
        .regions
        .iter()
        .find(|region| region.name == "Linker FLASH (xr)")
        .or_else(|| {
            model
                .regions
                .iter()
                .find(|region| region.name.starts_with("Linker FLASH"))
        })
        .unwrap();
    let ram = model
        .regions
        .iter()
        .find(|region| region.name.starts_with("Linker RAM"))
        .unwrap();
    assert_eq!((flash.start, flash.size), (0x0800_0000, 65536));
    assert_eq!((ram.start, ram.size), (0x2000_0000, 16384));
    assert!(flash.physical.is_none() && ram.physical.is_none());
    assert!(model.physical_resources.is_empty());
    assert!(model
        .static_spans
        .iter()
        .all(|span| span.kind != SpanKind::Free));
    let captured = load_inputs(
        &fixture.path("fixture.elf"),
        Some(&fixture.path("fixture.map")),
        &[fixture.path("exporter-snapshot.json")],
        None,
    )
    .unwrap();
    assert!(captured
        .snapshots
        .iter()
        .flat_map(|snapshot| &snapshot.spans)
        .all(|span| captured
            .regions
            .iter()
            .any(|region| region.id == span.region)));
}

#[test]
fn linker_budgets_preserve_captured_backing_and_journal_region_identity() {
    let fixture = Fixture::new();
    let snapshot_path = fixture.path("exporter-snapshot.json");
    let mut snapshot: serde_json::Value =
        serde_json::from_slice(&fs::read(&snapshot_path).unwrap()).unwrap();
    let region = &mut snapshot["regions"][0];
    let region_id = region["id"].as_str().unwrap().to_owned();
    let region_size = region["size"].as_u64().unwrap();
    region["physical"] = serde_json::json!({"id":"captured-bank","offset":0});
    snapshot["physical_resources"] = serde_json::json!([
        {"id":"captured-bank","name":"Captured backing","size":region_size}
    ]);
    fs::write(&snapshot_path, serde_json::to_vec(&snapshot).unwrap()).unwrap();
    let model = load_inputs(
        &fixture.path("fixture.elf"),
        Some(&fixture.path("fixture.map")),
        &[snapshot_path],
        None,
    )
    .unwrap();
    let retained = model
        .regions
        .iter()
        .find(|region| region.id == region_id)
        .unwrap();
    assert_eq!(retained.physical.as_ref().unwrap().id, "captured-bank");
    assert_eq!(retained.size, region_size);
    assert!(model.snapshots[0]
        .spans
        .iter()
        .all(|span| span.region == region_id));
    assert!(model
        .warnings
        .iter()
        .any(|warning| warning.contains("retained capture coordinates")));
    let journal_path = fixture.path("heap-journal.json");
    let mut journal: serde_json::Value =
        serde_json::from_slice(&fs::read(&journal_path).unwrap()).unwrap();
    journal["heap"]["region"] = serde_json::json!(region_id);
    fs::write(&journal_path, serde_json::to_vec(&journal).unwrap()).unwrap();
    load_heap_journal(&journal_path, &model, None).unwrap();
}

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

#[test]
fn linked_load_placements_match_readelf_and_aliases_count_once() {
    let fixture = Fixture::new();
    let elf = fixture.path("fixture.elf");
    let map = fixture.path("fixture.map");
    let layout_path = fixture.path("layout.json");
    let mut layout: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&layout_path).unwrap()).unwrap();
    // The explicit descriptor relates the load and CPU domains. Without it,
    // equal numeric addresses alone never establish shared physical storage.
    let mut load_regions = layout["regions"].as_array().unwrap().clone();
    for region in &mut load_regions {
        region["id"] = format!("load-{}", region["id"].as_str().unwrap()).into();
        region["domain"] = "linked_load".into();
    }
    layout["regions"]
        .as_array_mut()
        .unwrap()
        .extend(load_regions);
    fs::write(&layout_path, serde_json::to_vec(&layout).unwrap()).unwrap();
    let model = load_inputs(&elf, Some(&map), &[], Some(&layout_path)).unwrap();
    let output = Command::new("readelf")
        .arg("-lW")
        .arg(&elf)
        .output()
        .unwrap();
    assert!(output.status.success());
    let readelf = String::from_utf8(output.stdout).unwrap();
    let headers: Vec<Vec<u64>> = readelf
        .lines()
        .filter_map(|line| {
            let parts: Vec<_> = line.split_whitespace().collect();
            (parts.first() == Some(&"LOAD")).then(|| {
                parts[1..6]
                    .iter()
                    .map(|value| u64::from_str_radix(value.trim_start_matches("0x"), 16).unwrap())
                    .collect()
            })
        })
        .collect();
    let data_header = headers
        .iter()
        .find(|header| header[1] == 0x2000_0000)
        .unwrap();
    let data = model
        .load_mappings
        .iter()
        .find(|mapping| mapping.section == ".data")
        .unwrap();
    assert_eq!(data.runtime.start, data_header[1]);
    assert_eq!(data.load.start, data_header[2]);
    assert_eq!(data.image.start, data_header[0]);
    assert_eq!(data.runtime.size, 4);
    assert!(data.distinct_load_address());
    let text = model
        .load_mappings
        .iter()
        .find(|mapping| mapping.section == ".text")
        .unwrap();
    assert!(!text.distinct_load_address());
    assert!(model
        .load_mappings
        .iter()
        .all(|mapping| mapping.section != ".bss" && mapping.section != ".heap"));
    assert!(model
        .static_spans
        .iter()
        .filter(|span| {
            model
                .regions
                .iter()
                .find(|region| region.id == span.region)
                .unwrap()
                .domain
                == AddressDomain::LinkedLoad
        })
        .all(|span| span.name != ".bss" && span.name != ".heap"));
    // Includes the linker's alignment bytes as loaded storage, counts text/rodata
    // aliases once, and adds the distinct .data initializer exactly once.
    assert_eq!(
        model.physical_occupied_bytes(0)["fixture-flash"],
        headers.iter().map(|header| header[3]).sum::<u64>()
    );
    assert_eq!(
        model.physical_occupied_bytes(0)["fixture-sram"],
        4 + 128 + 4096
    );
    assert!(model
        .static_spans
        .iter()
        .any(|span| span.region == data.load.region
            && span.start == data.load.start
            && span.source.as_deref() == Some("application.o")));

    let mut bad = model.clone();
    bad.load_mappings[0].load.region = text.runtime.region.clone();
    assert!(
        bad.validate().is_err(),
        "CPU region accepted as linked load coordinates"
    );
    let mut bad = model.clone();
    bad.load_mappings[0].image.size += 1;
    assert!(bad.validate().is_err(), "unequal mapping extents accepted");
    let mut bad = model.clone();
    bad.load_mappings[0].load.start = u64::MAX;
    assert!(
        bad.validate().is_err(),
        "overflowing load placement accepted"
    );

    // A free CPU alias of an initializer is contradictory even though no CPU
    // section occupies that numeric address. A load reservation must not mask it.
    let cpu_flash = model
        .regions
        .iter()
        .find(|region| region.id == "fixture-flash-window")
        .unwrap();
    let capture = serde_json::json!({
        "schema_version": 1, "build": model.build,
        "regions": [cpu_flash], "physical_resources": model.physical_resources,
        "snapshots": [{"time_ns":0,"label":"negative control", "spans":[{
            "id":"contradictory-free","name":"not actually free","region":cpu_flash.id,
            "start":data.load.start,"size":data.load.size,"kind":"free"
        }]}]
    });
    let capture_path = fixture.path("contradictory-free.json");
    fs::write(&capture_path, serde_json::to_vec(&capture).unwrap()).unwrap();
    assert!(
        load_inputs(&elf, Some(&map), &[capture_path], Some(&layout_path))
            .unwrap_err()
            .to_string()
            .contains("contradictory free/occupied")
    );
}

#[test]
fn malformed_program_header_extents_are_rejected() {
    let fixture = Fixture::new();
    let bytes = fs::read(fixture.path("fixture.elf")).unwrap();
    // The independent host fixture is ELF64 on the Linux qualification runner.
    assert_eq!(&bytes[..6], b"\x7fELF\x02\x01");
    let phoff = u64::from_le_bytes(bytes[32..40].try_into().unwrap()) as usize;
    let stride = u16::from_le_bytes(bytes[54..56].try_into().unwrap()) as usize;
    let count = u16::from_le_bytes(bytes[56..58].try_into().unwrap()) as usize;
    let header = (0..count)
        .map(|index| phoff + stride * index)
        .find(|offset| u32::from_le_bytes(bytes[*offset..*offset + 4].try_into().unwrap()) == 1)
        .unwrap();
    let filesz = u64::from_le_bytes(bytes[header + 32..header + 40].try_into().unwrap());
    for (name, field_offset, value, expected) in [
        (
            "file-greater-than-memory",
            40,
            filesz - 1,
            "p_filesz greater than p_memsz",
        ),
        (
            "file-outside-elf",
            8,
            bytes.len() as u64 + 4096,
            "exceeds ELF file bounds",
        ),
        ("runtime-overflow", 16, u64::MAX - 7, "overflows u64"),
        ("load-overflow", 24, u64::MAX - 7, "overflows u64"),
        ("invalid-alignment", 48, 3, "invalid alignment"),
    ] {
        let mut bad = bytes.clone();
        // Avoid an unrelated alignment rejection masking the intended branch.
        bad[header + 48..header + 56].copy_from_slice(&1_u64.to_le_bytes());
        bad[header + field_offset..header + field_offset + 8].copy_from_slice(&value.to_le_bytes());
        let path = fixture.path(&format!("{name}.elf"));
        fs::write(&path, bad).unwrap();
        let error = load_inputs(&path, None, &[], None).unwrap_err().to_string();
        assert!(error.contains(expected), "{name}: {error}");
    }
}

#[test]
fn elf32_ranges_respect_the_target_address_width() {
    let fixture = Fixture::new();
    // Minimal independent ELF32/ARM fixture: one PT_LOAD, one 16-byte .text,
    // and a section-name table. Its fields exercise 32-bit address limits even
    // when the host linker and model's geometry both use 64-bit integers.
    let mut bytes = vec![0_u8; 0x200];
    bytes[..7].copy_from_slice(b"\x7fELF\x01\x01\x01");
    for (offset, value) in [
        (16, 2_u16),
        (18, 40),
        (40, 52),
        (42, 32),
        (44, 1),
        (46, 40),
        (48, 3),
        (50, 2),
    ] {
        bytes[offset..offset + 2].copy_from_slice(&value.to_le_bytes());
    }
    for (offset, value) in [
        (20, 1_u32),
        (24, 0x1000),
        (28, 52),
        (32, 0x180),
        (52, 1),
        (56, 0x100),
        (60, 0x1000),
        (64, 0x1000),
        (68, 16),
        (72, 16),
        (76, 5),
        (80, 1),
        (0x1a8, 1),
        (0x1ac, 1),
        (0x1b0, 6),
        (0x1b4, 0x1000),
        (0x1b8, 0x100),
        (0x1bc, 16),
        (0x1c8, 4),
        (0x1d0, 7),
        (0x1d4, 3),
        (0x1e0, 0x120),
        (0x1e4, 17),
        (0x1f0, 1),
    ] {
        bytes[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
    }
    bytes[0x120..0x131].copy_from_slice(b"\0.text\0.shstrtab\0");
    let path = fixture.path("minimal-arm.elf");
    fs::write(&path, &bytes).unwrap();
    let model = load_inputs(&path, None, &[], None).unwrap();
    assert_eq!(model.load_mappings.len(), 1);
    assert_eq!(model.load_mappings[0].load.start, 0x1000);
    for (offset, expected) in [
        (60, "ELF32 runtime"),
        (64, "ELF32 linked-load"),
        (0x1b4, "ELF32 address space"),
    ] {
        let mut bad = bytes.clone();
        bad[offset..offset + 4].copy_from_slice(&(u32::MAX - 7).to_le_bytes());
        fs::write(&path, bad).unwrap();
        let error = load_inputs(&path, None, &[], None).unwrap_err().to_string();
        assert!(error.contains(expected), "{error}");
    }
}
