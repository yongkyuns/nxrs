#[path = "../target_probe.rs"]
mod probe;
use csi_lab_core::motion_wire::Checkpoint;

fn record(bytes: &mut Vec<u8>, tag: u32, w: &[u32]) {
    probe::append(bytes, tag, w).unwrap();
}
fn basic() -> Vec<u8> {
    let mut bytes = probe::MAGIC.to_vec();
    record(&mut bytes, probe::BEGIN, &[1]);
    record(&mut bytes, probe::LABEL, &[0, 0, 1]);
    record(&mut bytes, probe::TRAIN_STOP, &[1, 0, 1]);
    for n in 1..=8u32 {
        let mut w = vec![
            n * 25_000,
            0,
            n,
            n,
            n * 25_000 - 1000,
            0,
            n * 25_000,
            0,
            1,
            1,
            4,
            0,
        ];
        w.extend([1.0f32, 2.0, 3.0, 4.0].map(f32::to_bits));
        record(&mut bytes, probe::SOURCE, &w);
    }
    record(&mut bytes, probe::END, &[201_000, 0]);
    bytes
}
#[test]
fn bounded_original_input_pipeline_has_no_admission_and_cases_reset_faults() {
    let bytes = basic();
    let mut updates = Vec::<Checkpoint>::new();
    let first = probe::execute(&bytes, |_, c| updates.push(c), |_, _| {}).unwrap();
    assert_eq!(first.cases, 1);
    assert_eq!(first.distances, 0);
    assert!(updates.iter().all(|c| c.words[3] == 0)); // Warmup/calibration stays unknown.
    assert!(updates.iter().any(|c| c.words[2] == 1));
    let mut combined = bytes.clone();
    let mut second = basic()[8..].to_vec();
    second[8..12].copy_from_slice(&2u32.to_le_bytes());
    combined.extend(second);
    let report = probe::execute(&combined, |_, _| {}, |_, _| {}).unwrap();
    assert_eq!(report.cases, 2);
    assert_eq!(report.checkpoints, first.checkpoints * 2);
    let mut a = Vec::new();
    let mut b = Vec::new();
    probe::run(&combined, &mut a).unwrap();
    probe::run(&combined, &mut b).unwrap();
    assert_eq!(a, b);
}
#[test]
fn corrupt_shapes_boundaries_and_native_answers_produce_no_output_prefix() {
    let valid = basic();
    let mut bad = Vec::new();
    bad.push(valid[..valid.len() - 1].to_vec());
    let mut duplicate = valid.clone();
    duplicate.extend(&valid[8..]);
    bad.push(duplicate);
    for (tag, w) in [
        (probe::CLOCK, vec![1, 0, 2]),
        (probe::LABEL, vec![1, 0, 4]),
        (probe::SOURCE, vec![1, 0, 1, 1, 1, 0, 1, 0, 1, 1, 17, 0]),
        (
            probe::DISTANCE,
            vec![
                2,
                1,
                1.0f32.to_bits(),
                2.0f32.to_bits(),
                1.0f32.to_bits(),
                2.0f32.to_bits(),
            ],
        ),
        (99, vec![]),
    ] {
        let mut bytes = probe::MAGIC.to_vec();
        record(&mut bytes, probe::BEGIN, &[1]);
        record(&mut bytes, tag, &w);
        record(&mut bytes, probe::END, &[2, 0]);
        bad.push(bytes);
    }
    for bytes in bad {
        let mut output = Vec::new();
        assert!(probe::run(&bytes, &mut output).is_err());
        assert!(output.is_empty());
    }
    let mut oversized = probe::MAGIC.to_vec();
    oversized.extend([0; 16]);
    oversized[12..16].copy_from_slice(&u32::MAX.to_le_bytes());
    assert!(probe::execute(&oversized, |_, _| {}, |_, _| {}).is_err());
}
