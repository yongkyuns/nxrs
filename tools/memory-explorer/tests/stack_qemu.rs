//! Read-only native stack observations checked against independently retained
//! raw stack memory and ARM saved-context bytes from the lifecycle witness.

use nxrs_memory_explorer::{
    load_inputs, StackCurrent, StackGrowth, StackHighWater, StackPointerSource,
};
use object::{Object, ObjectSection};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};

mod support;
use support::source_pins;

fn json(path: &Path) -> Value {
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}

fn sha256(path: &Path) -> String {
    format!("{:x}", Sha256::digest(fs::read(path).unwrap()))
}

fn number(value: &Value, key: &str) -> u64 {
    value[key].as_u64().unwrap()
}

fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value[key].as_str().unwrap()
}

fn decode_hex(value: &Value) -> Vec<u8> {
    let value = value.as_str().unwrap();
    assert_eq!(value.len() % 2, 0);
    let (pairs, _) = value.as_bytes().as_chunks::<2>();
    pairs
        .iter()
        .map(|pair| u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap())
        .collect()
}

#[test]
#[ignore = "requires -g3 native timing build and fresh lifecycle capture via NXRS_STACK_QEMU_* env"]
fn stack_depth_and_coloration_match_independent_stopped_bytes() {
    let build_dir = PathBuf::from(std::env::var_os("NXRS_STACK_QEMU_BUILD").unwrap());
    let evidence = PathBuf::from(std::env::var_os("NXRS_STACK_QEMU_EVIDENCE").unwrap());
    let tools = Path::new(env!("CARGO_MANIFEST_DIR"));
    let repo = tools.join("../..");
    let build = json(&build_dir.join("build.json"));
    let report = json(&evidence.join("report.json"));
    let run = json(&evidence.join("run.json"));
    let image = build_dir.join("nuttx");
    let elf_hash = sha256(&image);
    assert_eq!(build["elf_sha256"], elf_hash);
    assert_eq!(report["elf_sha256"], elf_hash);
    assert_eq!(run["elf_sha256"], elf_hash);
    let config_hash = sha256(&build_dir.join("resolved.config"));
    assert_eq!(build["config_sha256"], config_hash);
    assert_eq!(report["config_sha256"], config_hash);
    assert_eq!(sha256(&evidence.join("resolved.config")), config_hash);
    let pins = source_pins();
    assert_eq!(build["nuttx_revision"], pins["nuttx"]);
    assert_eq!(build["apps_revision"], pins["apps"]);
    assert_eq!(
        build["configuration_checks"]["DEBUG_SYMBOLS_LEVEL"]["effective"],
        "\"-g3\""
    );
    for name in ["nuttx", "apps"] {
        assert_eq!(
            build["source_identity"][name]["tracked_sources_clean"],
            true
        );
    }
    for (name, hash) in build["fixture_sha256"].as_object().unwrap() {
        assert_eq!(
            hash,
            &sha256(&repo.join("tests/nuttx-std/memory-timing").join(name))
        );
    }
    for name in [
        "gdb_export.py",
        "tests/qemu_capture_gdb.py",
        "tests/lifecycle_capture_gdb.py",
        "tests/qualify_lifecycle_qemu.py",
    ] {
        assert_eq!(
            report["capture_sources_sha256"][name],
            sha256(&tools.join(name))
        );
        assert_eq!(
            run["capture_sources_sha256"][name],
            report["capture_sources_sha256"][name]
        );
    }
    let image_bytes = fs::read(&image).unwrap();
    let elf = object::File::parse(image_bytes.as_slice()).unwrap();
    assert!(
        elf.is_little_endian(),
        "the raw stack oracle is MPS2-specific"
    );
    assert!(elf.section_by_name(".debug_macro").unwrap().size() > 0);
    assert_eq!(report["evidence"], "real_nuttx_qemu_linked_lifecycle");
    assert_eq!(report["checks"]["target_function_calls"], false);
    let points = report["snapshots"].as_array().unwrap();
    assert_eq!(points.len(), 4);
    let paths: Vec<_> = points
        .iter()
        .map(|point| evidence.join(text(point, "capture_file")))
        .collect();
    let model = load_inputs(&image, None, &paths, None).unwrap();
    let (mut live, mut saved, mut scans) = (0, 0, 0);
    for point in points {
        assert_eq!(
            point["metadata_unchanged"], true,
            "raw stack bytes and registers must be unchanged across export"
        );
        assert_eq!(point["timer_unchanged_while_stopped"], true);
        let raw = json(&evidence.join(text(point, "raw_file")));
        assert_eq!(raw["elf_sha256"], elf_hash);
        let snapshot = model.snapshot_at(number(point, "time_ns")).unwrap();
        assert!(!snapshot.stack_observations.is_empty());
        let mut expected_tcbs: Vec<_> = raw["tasks"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|task| number(task, "stack_base_ptr") > 0 && number(task, "adj_stack_size") > 0)
            .map(|task| number(task, "tcb"))
            .collect();
        let mut observed_tcbs: Vec<_> = snapshot
            .stack_observations
            .iter()
            .map(|observation| observation.tcb)
            .collect();
        expected_tcbs.sort_unstable();
        observed_tcbs.sort_unstable();
        assert_eq!(
            observed_tcbs, expected_tcbs,
            "every independently observed task stack must have one observation"
        );
        let (mut checkpoint_live, mut checkpoint_saved, mut checkpoint_scans) = (0, 0, 0);
        for observation in &snapshot.stack_observations {
            let task = raw["tasks"]
                .as_array()
                .unwrap()
                .iter()
                .find(|task| number(task, "tcb") == observation.tcb)
                .unwrap();
            assert_eq!(u64::from(observation.task_id), number(task, "pid"));
            assert_eq!(observation.growth, StackGrowth::Down);
            let stack = snapshot
                .spans
                .iter()
                .find(|span| span.id == observation.stack)
                .unwrap();
            assert_eq!(stack.start, number(task, "stack_base_ptr"));
            assert_eq!(stack.size, number(task, "adj_stack_size"));
            let bytes = &task["stack_bytes"];
            let sp = number(bytes, "sp");
            match observation.current {
                StackCurrent::Observed {
                    sp: observed,
                    source,
                } => {
                    assert_eq!(observed, sp);
                    assert_eq!(
                        observation.current_used(stack),
                        Some(stack.start + stack.size - sp)
                    );
                    assert_eq!(observation.current_headroom(stack), Some(sp - stack.start));
                    match source {
                        StackPointerSource::LiveRegister => {
                            assert_eq!(bytes["sp_source"], "live_register");
                            assert_eq!(observation.tcb, number(bytes, "running_tcb"));
                            assert_eq!(number(bytes, "xpsr") & 0x1ff, 0);
                            checkpoint_live += 1;
                        }
                        StackPointerSource::SavedContext => {
                            assert_eq!(bytes["sp_source"], "saved_context");
                            assert_ne!(observation.tcb, number(bytes, "running_tcb"));
                            let word: [u8; 4] =
                                decode_hex(&bytes["saved_sp_word_hex"]).try_into().unwrap();
                            assert_eq!(sp, u64::from(u32::from_le_bytes(word)));
                            assert!(number(bytes, "saved_context_address") > 0);
                            checkpoint_saved += 1;
                        }
                    }
                }
                StackCurrent::Unavailable { ref reason } => {
                    panic!("pinned native fixture lost an observed task SP: {reason}");
                }
            }
            if let StackHighWater::Coloration {
                scan_start,
                scan_size,
                used_bytes,
                pattern,
                word_bytes,
            } = observation.high_water
            {
                // Derived from raw target bytes and the pinned ARM native scan,
                // never from the exporter's computed watermark.
                assert_eq!(scan_start, (stack.start + 7) & !7);
                assert_eq!(scan_size, ((stack.start + stack.size) & !7) - scan_start);
                assert_eq!(word_bytes, 4);
                assert_eq!(pattern, 0xdeadbeef);
                let data = decode_hex(&bytes["bytes_hex"]);
                assert_eq!(data.len() as u64, scan_size);
                assert_eq!(
                    format!("{:x}", Sha256::digest(&data)),
                    text(bytes, "bytes_sha256")
                );
                let (words, _) = data.as_chunks::<4>();
                let untouched_words = words
                    .iter()
                    .take_while(|word| u32::from_le_bytes(**word) == 0xdeadbeef)
                    .count();
                let untouched = untouched_words as u64 * 4;
                assert_eq!(used_bytes, scan_size - untouched);
                assert_eq!(observation.watermark_used(), Some(used_bytes));
                assert_eq!(observation.watermark_headroom(), Some(untouched));
                checkpoint_scans += 1;
            } else if let StackHighWater::Unavailable { ref reason } = observation.high_water {
                panic!("pinned -g3 fixture lost a coloration scan: {reason}");
            }
        }
        assert_eq!(
            checkpoint_live, 1,
            "require the actual stopped task register"
        );
        assert!(
            checkpoint_saved > 0,
            "require independently read nonrunning contexts"
        );
        assert_eq!(
            checkpoint_live + checkpoint_saved,
            snapshot.stack_observations.len()
        );
        assert_eq!(
            checkpoint_scans,
            snapshot.stack_observations.len(),
            "every represented fixture stack must retain coloration"
        );
        live += checkpoint_live;
        saved += checkpoint_saved;
        scans += checkpoint_scans;
    }
    println!("Native stack gate passed: {} checkpoints, {live} live SPs, {saved} saved SPs, {scans} independently decoded coloration scans; stopped bytes unchanged.", points.len());
}
