use serde_json::{json, Value};
use std::{
    fs,
    io::Write,
    path::PathBuf,
    process::{Command, Stdio},
    sync::atomic::{AtomicU32, Ordering},
};
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        static NEXT: AtomicU32 = AtomicU32::new(0);
        let p = std::env::temp_dir().join(format!(
            "csi-lab-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&p).unwrap();
        Self(p)
    }
    fn path(&self, name: &str) -> String {
        self.0.join(name).to_str().unwrap().to_owned()
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
fn run(args: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_csi-lab"))
        .args(args)
        .output()
        .unwrap()
}
fn capture(temp: &Temp, input: &[u8]) -> (String, std::process::Output) {
    let manifest = temp.path("manifest.json");
    fs::write(&manifest,json!({"source_commit":"0".repeat(40),"sdk_revision":"0".repeat(40),"firmware_sha256":"0".repeat(64),"origin":"synthetic_test","scope":"calibration_and_numeric_inputs_v1"}).to_string()).unwrap();
    let output = temp.path("trial.csilab");
    let mut child = Command::new(env!("CARGO_BIN_EXE_csi-lab"))
        .args(["record", &manifest, &output])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child.stdin.take().unwrap().write_all(input).unwrap();
    (output, child.wait_with_output().unwrap())
}
#[test]
fn record_replay_is_byte_identical_and_evaluation_is_explicit() {
    let temp = Temp::new();
    let fixture = run(&["fixture"]);
    assert!(fixture.status.success());
    let (path, record) = capture(&temp, &fixture.stdout);
    assert!(
        record.status.success(),
        "{}",
        String::from_utf8_lossy(&record.stderr)
    );
    let a = run(&["replay", &path]);
    let b = run(&["replay", &path]);
    assert!(a.status.success(), "{}", String::from_utf8_lossy(&a.stderr));
    assert_eq!(a.stdout, b.stdout);
    assert_eq!(a.stderr, b.stderr);
    let values: Vec<Value> = String::from_utf8(a.stdout)
        .unwrap()
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect();
    assert!(values.iter().any(|v| v["state"] == "presence_hint"));
    assert_eq!(values.last().unwrap()["rejection"], "progress_expired");
    let e = run(&["evaluate", &path, "--sensitivity", "0.5"]);
    assert!(e.status.success());
    assert!(String::from_utf8(e.stdout)
        .unwrap()
        .lines()
        .all(|s| serde_json::from_str::<Value>(s).unwrap()["evaluation"] == true));
    let export = temp.path("kernels.jsonl");
    let native = run(&["export-kernels", &path, &export]);
    assert!(
        native.status.success(),
        "{}",
        String::from_utf8_lossy(&native.stderr)
    );
    let calls = fs::read_to_string(export).unwrap();
    assert_eq!(calls.lines().count(), 2);
    assert_eq!(
        serde_json::from_str::<Value>(calls.lines().nth(1).unwrap()).unwrap()["kind"],
        "wander"
    );
    let overwrite = run(&["record", &temp.path("manifest.json"), &path]);
    assert!(!overwrite.status.success());
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            fs::metadata(path).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
}
#[test]
fn corrupt_or_truncated_capture_emits_no_replayed_states() {
    for corrupt in [false, true] {
        let temp = Temp::new();
        let fixture = run(&["fixture"]);
        let (path, result) = capture(&temp, &fixture.stdout);
        assert!(result.status.success());
        let mut bytes = fs::read(&path).unwrap();
        if corrupt {
            let pos = bytes.iter().position(|v| *v == b'\n').unwrap() + 8 + 70;
            bytes[pos] = if bytes[pos] == b'0' { b'1' } else { b'0' };
        } else {
            bytes.truncate(bytes.len() - 30);
        }
        fs::write(&path, bytes).unwrap();
        let r = run(&["replay", &path]);
        assert!(!r.status.success());
        assert!(r.stdout.is_empty());
    }
}
#[test]
fn incomplete_console_capture_never_claims_success() {
    let temp = Temp::new();
    let fixture = run(&["fixture"]);
    let (_, r) = capture(&temp, &fixture.stdout[..fixture.stdout.len() - 90]);
    assert!(!r.status.success());
}
#[test]
fn standalone_runtime_witness_uses_the_shared_core() {
    let r = Command::new(env!("CARGO_BIN_EXE_calibration-runtime-witness"))
        .output()
        .unwrap();
    assert!(r.status.success());
    assert!(String::from_utf8(r.stdout)
        .unwrap()
        .contains("reader_preempted_before_publication"));
}

fn hardware_header(path: &str) {
    let text = fs::read_to_string(path).unwrap();
    let mut lines = text.lines();
    let mut h: Value = serde_json::from_str(lines.next().unwrap()).unwrap();
    h["manifest"]["origin"] = json!("hardware");
    fs::write(
        path,
        format!("{h}\n{}\n", lines.collect::<Vec<_>>().join("\n")),
    )
    .unwrap();
}
#[test]
fn hardware_replay_requires_an_explicit_selected_source() {
    let temp = Temp::new();
    let fixture = run(&["fixture"]);
    let (path, r) = capture(&temp, &fixture.stdout);
    assert!(r.status.success());
    hardware_header(&path);
    let r = run(&["replay", &path]);
    assert!(!r.status.success());
    assert!(r.stdout.is_empty());
    assert!(String::from_utf8(r.stderr)
        .unwrap()
        .contains("selected source identity"));
}
#[test]
fn selected_peer_and_channel_are_part_of_the_capture_epoch() {
    use csi_lab_core::{
        calibration::Config,
        journal::{self, Record},
        recorder::Recorder,
    };
    let temp = Temp::new();
    let recorder = Recorder::new();
    assert!(recorder.prepare(Config::EMPTY_HOLDOUT, 8));
    recorder.push(Record::new(journal::CONTROL, 1, &[3, 6, 2, 0x100]).unwrap());
    let mut transport = Vec::new();
    recorder.finish(2, &mut transport).unwrap();
    let (path, r) = capture(&temp, &transport);
    assert!(r.status.success());
    hardware_header(&path);
    let r = run(&["replay", &path]);
    assert!(r.status.success(), "{}", String::from_utf8_lossy(&r.stderr));
}

#[test]
fn packed_native_call_round_trips_through_capture_replay_and_export() {
    use csi_lab_core::{calibration::Config, recorder::Recorder};
    let r = Recorder::new();
    assert!(r.prepare(Config::EMPTY_HOLDOUT, 4));
    let lease = r.lease().unwrap();
    assert_eq!(lease.wander(1, 1, &[1.0, 2.0], &[3.0, 4.0], || 0.25), 0.25);
    drop(lease);
    let mut wire = Vec::new();
    r.finish(2, &mut wire).unwrap();
    let temp = Temp::new();
    let (path, result) = capture(&temp, &wire);
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let a = run(&["replay", &path]);
    let b = run(&["replay", &path]);
    assert!(a.status.success());
    assert_eq!(a.stdout, b.stdout);
    assert_eq!(a.stderr, b.stderr);
    let output = temp.path("packed-kernels.jsonl");
    let out = run(&["export-kernels", &path, &output]);
    assert!(out.status.success());
    let text = fs::read_to_string(output).unwrap();
    let call: Value = serde_json::from_str(text.lines().nth(1).unwrap()).unwrap();
    assert_eq!(
        call["input0_bits"],
        json!([1.0f32.to_bits(), 2.0f32.to_bits()])
    );
    assert_eq!(
        call["input1_bits"],
        json!([3.0f32.to_bits(), 4.0f32.to_bits()])
    );
    assert_eq!(call["output_bits"], json!([0.25f32.to_bits()]));
}

#[test]
fn preempted_recording_poll_loses_source_and_replays_the_sticky_fault() {
    use csi_lab_core::{
        calibration::{Config, Feature, Phase, Rejection, Runtime},
        journal::{Operation, Record, MODEL},
        recorder::Recorder,
    };
    let runtime = Runtime::new();
    assert!(runtime.prepare(Config::EMPTY_HOLDOUT));
    let recorder = Recorder::new();
    assert!(recorder.prepare(Config::EMPTY_HOLDOUT, 8));
    let admitted = std::sync::Barrier::new(2);
    let resume = std::sync::Barrier::new(2);
    let feature = Feature {
        sequence: 1,
        oldest_us: 11,
        newest_us: 11,
        templates: 10,
        training_complete: true,
        gain_available: true,
        wander: 0.01,
    };
    std::thread::scope(|scope| {
        let reader = scope.spawn(|| {
            runtime
                .execute(
                    Operation::Poll,
                    || 10,
                    |event| {
                        admitted.wait();
                        resume.wait();
                        recorder.push_model(event);
                    },
                )
                .unwrap()
        });
        admitted.wait();
        assert!(runtime
            .execute(
                Operation::Observe(feature),
                || panic!("lost source must not sample time"),
                |_| panic!("lost observation has no checkpoint")
            )
            .is_none());
        resume.wait();
        reader.join().unwrap();
    });
    let (_, state) = runtime
        .execute(
            Operation::Observe(Feature {
                sequence: 2,
                oldest_us: 12,
                newest_us: 12,
                ..feature
            }),
            || 12,
            |event| recorder.push_model(event),
        )
        .unwrap();
    assert_eq!(state.phase, Phase::Rejected);
    assert_eq!(state.rejection, Rejection::SourceContention);
    assert_eq!(recorder.loss(), 0); // Recording integrity is separate from source admission.
    let mut wire = Vec::new();
    recorder.finish(13, &mut wire).unwrap();
    let rows: Vec<_> = std::str::from_utf8(&wire)
        .unwrap()
        .lines()
        .map(|line| Record::parse(line).unwrap())
        .filter(|r| r.kind == MODEL)
        .collect();
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0].words[6], 0);
    assert_eq!(rows[1].words[1], 3);
    assert_eq!(rows[1].words[2], 3);
    let temp = Temp::new();
    let (path, result) = capture(&temp, &wire);
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let a = run(&["replay", &path]);
    let b = run(&["replay", &path]);
    assert!(a.status.success());
    assert_eq!(a.stdout, b.stdout);
    assert_eq!(a.stderr, b.stderr);
    let text = String::from_utf8(a.stdout).unwrap();
    assert_eq!(
        serde_json::from_str::<Value>(text.lines().last().unwrap()).unwrap()["rejection"],
        "source_contention"
    );
}

#[test]
fn sole_owner_records_queued_sources_and_labels_while_poll_is_preempted() {
    use csi_lab_core::{
        calibration::{Config, Feature, Phase, Rejection, Runtime, State},
        journal::{self, Operation, Record, MODEL},
        ownership::{Control, Environment, Evidence, Marker},
        recorder::Recorder,
    };
    use std::{
        sync::{
            atomic::{AtomicBool, AtomicU64},
            Arc, Barrier,
        },
        time::Duration,
    };
    #[derive(Clone)]
    struct Hooks {
        clock: Arc<AtomicU64>,
        fault: Arc<Runtime>,
        recorder: Arc<Recorder>,
        gate: Arc<(AtomicBool, Barrier, Barrier)>,
    }
    impl Environment for Hooks {
        fn now_us(&self) -> u64 {
            self.clock.load(Ordering::Relaxed)
        }
        fn failure(&self) -> Rejection {
            self.fault.failure_reason()
        }
        fn fail(&self, r: Rejection) {
            self.fault.fail(r);
        }
        fn record(&self, e: Evidence) {
            match e {
                Evidence::MotionCase | Evidence::Motion(_) => {
                    panic!("historical case emitted motion evidence")
                }
                Evidence::Model(e) => {
                    if e.words[0] == 5 && self.gate.0.swap(false, Ordering::SeqCst) {
                        self.gate.1.wait();
                        self.gate.2.wait();
                    }
                    self.recorder.push_model(e);
                }
                Evidence::Marker { time_us, marker } => {
                    let record = match marker {
                        Marker::Label(n) => Record::new(journal::LABEL, time_us, &[n]),
                        Marker::Training {
                            command,
                            status,
                            wander,
                            jitter,
                        } => Record::new(
                            journal::CONTROL,
                            time_us,
                            &[command, status as u32, wander.to_bits(), jitter.to_bits()],
                        ),
                    }
                    .unwrap();
                    self.recorder.push(record);
                }
            }
        }
    }
    let config = Config {
        minimum_features: 25,
        minimum_duration_us: 1000,
        ..Config::EMPTY_HOLDOUT
    };
    let hooks = Hooks {
        clock: Arc::new(AtomicU64::new(1)),
        fault: Arc::new(Runtime::new()),
        recorder: Arc::new(Recorder::new()),
        gate: Arc::new((AtomicBool::new(false), Barrier::new(2), Barrier::new(2))),
    };
    assert!(hooks.fault.prepare(config));
    assert!(hooks.recorder.prepare(config, 128));
    let (mut owner, source) =
        Control::new(config, 64, Duration::from_secs(1), hooks.clone()).unwrap();
    owner.marker(Marker::Label(1)).unwrap();
    owner
        .marker(Marker::Training {
            command: 2,
            status: 0,
            wander: 0.01,
            jitter: 0.01,
        })
        .unwrap();
    assert!(owner.operation(Operation::Start(10)).unwrap().0);
    let feature = |n| Feature {
        sequence: n,
        oldest_us: u64::from(n) * 1000,
        newest_us: u64::from(n) * 1000,
        templates: 10,
        training_complete: true,
        gain_available: true,
        wander: 0.01,
    };
    for n in 1..=100 {
        if n == 51 {
            assert!(owner.operation(Operation::Validate).unwrap().0);
        }
        hooks.clock.store(u64::from(n) * 1000, Ordering::Relaxed);
        source.observe(feature(n), n, |r| hooks.fail(r)).unwrap();
        owner.drain_ready().unwrap();
    }
    assert!(owner.operation(Operation::Accept).unwrap().0);
    hooks.clock.store(101000, Ordering::Relaxed);
    source
        .observe(feature(101), 101, |r| hooks.fail(r))
        .unwrap();
    owner.drain_ready().unwrap();
    hooks.gate.0.store(true, Ordering::SeqCst);
    std::thread::scope(|scope| {
        let reader = scope.spawn(move || {
            let state = owner.operation(Operation::Poll).unwrap().1;
            (owner, state)
        });
        hooks.gate.1.wait();
        hooks.clock.store(102000, Ordering::Relaxed);
        source
            .observe(feature(102), 102, |r| hooks.fail(r))
            .unwrap();
        assert_eq!(hooks.failure(), Rejection::None);
        hooks.gate.2.wait();
        let (mut owner, state) = reader.join().unwrap();
        assert_eq!(state.state, State::NotDetected);
        // A label must follow the accepted source prefix, even when no normal
        // drain iteration has run since the paused poll.
        owner.marker(Marker::Label(2)).unwrap();
        let snapshot = owner.operation(Operation::Poll).unwrap().1;
        assert_eq!(snapshot.phase, Phase::Ready);
        assert_eq!(snapshot.rejection, Rejection::None);
        owner.close().unwrap();
        assert_eq!(owner.counts(), (102, 102));
    });
    assert_eq!(hooks.recorder.loss(), 0);
    let mut wire = Vec::new();
    hooks.recorder.finish(103000, &mut wire).unwrap();
    let rows: Vec<_> = std::str::from_utf8(&wire)
        .unwrap()
        .lines()
        .map(|line| Record::parse(line).unwrap())
        .collect();
    let observed: Vec<_> = rows
        .iter()
        .filter(|r| r.kind == MODEL && r.words[0] == 4)
        .collect();
    assert_eq!(observed.len(), 102);
    assert!(observed
        .iter()
        .enumerate()
        .all(|(i, r)| r.words[23] == i as u32 + 1));
    let source_index = rows
        .iter()
        .position(|r| r.kind == MODEL && r.words[23] == 102)
        .unwrap();
    let label_index = rows
        .iter()
        .position(|r| r.kind == journal::LABEL && r.words[0] == 2)
        .unwrap();
    assert!(source_index < label_index);
    let temp = Temp::new();
    let (path, result) = capture(&temp, &wire);
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let a = run(&["replay", &path]);
    let b = run(&["replay", &path]);
    assert!(a.status.success(), "{}", String::from_utf8_lossy(&a.stderr));
    assert_eq!(a.stdout, b.stdout);
    assert_eq!(a.stderr, b.stderr);
    let text = String::from_utf8(a.stdout).unwrap();
    let last: Value = serde_json::from_str(text.lines().last().unwrap()).unwrap();
    assert_eq!(last["state"], "not_detected");
    assert_eq!(last["rejection"], "none");
}

fn vector_transport(fault_at: Option<u32>, window_ids: bool) -> Vec<u8> {
    use csi_lab_core::{
        calibration::{Config, Feature, Rejection, Runtime},
        journal::{self, Operation, Record},
        recorder::Recorder,
    };
    let runtime = Runtime::new();
    assert!(runtime.prepare(Config::EMPTY_HOLDOUT));
    let recorder = Recorder::new();
    assert!(recorder.prepare(Config::EMPTY_HOLDOUT, 128));
    recorder.push(Record::new(journal::LABEL, 1, &[1]).unwrap());
    for n in 1..=8u32 {
        let values = if n <= 3 {
            [1.0f32, 2.0, 3.0, 4.0]
        } else {
            [1.0f32, 4.0, 2.0, 3.0]
        };
        for part in 0..3 {
            let data = if part == 1 { &[][..] } else { &values[..] };
            let mut w = vec![n, part, 4, 1, 0, data.len() as u32, 0, 0];
            w.extend(data.iter().map(|v| v.to_bits()));
            recorder.push(Record::new(journal::MATRIX, u64::from(n) * 1000, &w).unwrap());
        }
        if fault_at == Some(n) {
            runtime.fail(Rejection::SourceContention);
        }
        runtime
            .execute(
                Operation::Observe(Feature {
                    sequence: n,
                    oldest_us: u64::from(n) * 1000 - 100,
                    newest_us: u64::from(n) * 1000,
                    templates: 1,
                    training_complete: false,
                    gain_available: true,
                    wander: 0.01,
                }),
                || u64::from(n) * 1000,
                |mut e| {
                    if window_ids {
                        e.words[23] = n;
                    }
                    recorder.push_model(e);
                },
            )
            .unwrap();
    }
    let mut wire = Vec::new();
    recorder.finish(9000, &mut wire).unwrap();
    wire
}

#[test]
fn derived_signals_keep_original_capture_and_never_admit_a_detector() {
    let temp = Temp::new();
    let (path, r) = capture(&temp, &vector_transport(None, true));
    assert!(r.status.success());
    let before = fs::read(&path).unwrap();
    let args = ["signals", &path, "--case", "recent-three-v1"];
    let a = run(&args);
    let b = run(&args);
    assert!(a.status.success(), "{}", String::from_utf8_lossy(&a.stderr));
    assert_eq!(a.stdout, b.stdout);
    assert_eq!(a.stderr, b.stderr);
    let rows: Vec<Value> = String::from_utf8(a.stdout)
        .unwrap()
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect();
    assert_eq!(rows.len(), 8);
    assert!(rows.iter().all(|v| v["evaluation"] == true
        && v["detector_admitted"] == false
        && v["original_capture_verified"] == true
        && v["label"] == "empty"));
    assert!(rows[..3]
        .iter()
        .all(|v| v["available"] == false && v["reason"] == "warmup"));
    assert!(f32::from_bits(rows[3]["score_bits"].as_u64().unwrap() as u32) > 0.5);
    assert_eq!(rows[3]["window"], 4);
    assert_eq!(rows[3]["oldest_us"], 900);
    assert_eq!(rows[6]["score_bits"], 0);
    assert_eq!(fs::read(path).unwrap(), before);
}

#[test]
fn counterfactual_signals_preserve_real_source_failure_as_unavailable() {
    let temp = Temp::new();
    let (path, r) = capture(&temp, &vector_transport(Some(5), true));
    assert!(r.status.success());
    let r = run(&["signals", &path, "--case", "recent-three-v1"]);
    assert!(r.status.success());
    let rows: Vec<Value> = String::from_utf8(r.stdout)
        .unwrap()
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect();
    assert_eq!(rows[3]["available"], true);
    assert!(rows[4..].iter().all(|v| v["available"] == false
        && v["score_bits"].is_null()
        && v["reason"] == "recorded_external_fault:3"));
}

#[test]
fn signals_reject_missing_identity_or_corruption_before_emitting_any_prefix() {
    for missing in [true, false] {
        let temp = Temp::new();
        let (path, r) = capture(&temp, &vector_transport(None, !missing));
        assert!(r.status.success());
        if !missing {
            let mut bytes = fs::read(&path).unwrap();
            bytes.truncate(bytes.len() - 40);
            fs::write(&path, bytes).unwrap();
        }
        for (command, case) in [
            ("signals", "recent-three-v1"),
            ("preview-motion", "temporal-motion-v1"),
        ] {
            let r = run(&[command, &path, "--case", case]);
            assert!(!r.status.success());
            assert!(r.stdout.is_empty());
        }
    }
}

#[test]
fn numerical_distance_witness_requires_exact_recorded_native_bits() {
    use csi_lab_core::{calibration::Config, recorder::Recorder};
    for result in [0.0, 0.25] {
        let recorder = Recorder::new();
        assert!(recorder.prepare(Config::EMPTY_HOLDOUT, 4));
        let lease = recorder.lease().unwrap();
        lease.wander(1, 1, &[1.0, 2.0], &[3.0, 4.0], || result);
        drop(lease);
        let mut wire = Vec::new();
        recorder.finish(2, &mut wire).unwrap();
        let temp = Temp::new();
        let (path, r) = capture(&temp, &wire);
        assert!(r.status.success());
        let r = run(&["verify-distance", &path]);
        if result == 0.0 {
            assert!(r.status.success());
            let v: Value = serde_json::from_slice(&r.stdout).unwrap();
            assert_eq!(v["native_comparisons"], 1);
            assert_eq!(v["all_result_bits_exact"], true);
        } else {
            assert!(!r.status.success());
            assert!(r.stdout.is_empty());
        }
    }
}

#[derive(Clone, Copy)]
enum MotionFixture {
    Ordinary,
    SourceFailure,
    Silence,
    HoldoutMotion,
    Device,
    DeviceFault,
    DeviceSilence,
    DeviceHoldout,
}
fn motion_policy_transport(case: MotionFixture) -> Vec<u8> {
    use csi_lab_core::{
        calibration::{Config, Feature, Rejection, Runtime},
        journal::{self, Operation, Record},
        recorder::Recorder,
    };
    let runtime = Runtime::new();
    assert!(runtime.prepare(Config::EMPTY_HOLDOUT));
    let recorder = Recorder::new();
    assert!(recorder.prepare(Config::EMPTY_HOLDOUT, 128));
    if !matches!(
        case,
        MotionFixture::Device
            | MotionFixture::DeviceFault
            | MotionFixture::DeviceSilence
            | MotionFixture::DeviceHoldout
    ) {
        recorder.push(Record::new(journal::LABEL, 0, &[1]).unwrap());
    }
    struct MotionHooks<'a> {
        clock: &'a std::sync::atomic::AtomicU64,
        runtime: &'a Runtime,
        recorder: &'a Recorder,
    }
    impl csi_lab_core::ownership::Environment for MotionHooks<'_> {
        fn now_us(&self) -> u64 {
            self.clock.load(Ordering::Relaxed)
        }
        fn failure(&self) -> Rejection {
            self.runtime.failure_reason()
        }
        fn fail(&self, r: Rejection) {
            self.runtime.fail(r);
        }
        fn record(&self, e: csi_lab_core::ownership::Evidence) {
            use csi_lab_core::ownership::{Evidence, Marker};
            match e {
                Evidence::MotionCase => self
                    .recorder
                    .push(Record::new(journal::CONTROL, self.now_us(), &[4, 1, 16, 0]).unwrap()),
                Evidence::Model(e) => self.recorder.push_model(e),
                Evidence::Motion(e) => {
                    let c = csi_lab_core::motion_wire::Checkpoint::from(e);
                    self.recorder
                        .push(Record::new(journal::MOTION, c.time_us, &c.words).unwrap());
                }
                Evidence::Marker { time_us, marker } => {
                    let r = match marker {
                        Marker::Label(n) => Record::new(journal::LABEL, time_us, &[n]),
                        Marker::Training {
                            command,
                            status,
                            wander,
                            jitter,
                        } => Record::new(
                            journal::CONTROL,
                            time_us,
                            &[command, status as u32, wander.to_bits(), jitter.to_bits()],
                        ),
                    };
                    self.recorder.push(r.unwrap());
                }
            }
        }
    }
    let device = matches!(
        case,
        MotionFixture::Device
            | MotionFixture::DeviceFault
            | MotionFixture::DeviceSilence
            | MotionFixture::DeviceHoldout
    );
    let clock = std::sync::atomic::AtomicU64::new(0);
    let make = if device {
        csi_lab_core::ownership::Control::new_motion_trial
    } else {
        csi_lab_core::ownership::Control::new
    };
    let (mut owner, ingress) = make(
        Config::EMPTY_HOLDOUT,
        64,
        std::time::Duration::from_secs(1),
        MotionHooks {
            clock: &clock,
            runtime: &runtime,
            recorder: &recorder,
        },
    )
    .unwrap();
    if device {
        owner
            .marker(csi_lab_core::ownership::Marker::Label(1))
            .unwrap();
    }
    let mut wire = Vec::new();
    let frames: u32 = match case {
        MotionFixture::Silence | MotionFixture::DeviceSilence => 4400,
        MotionFixture::HoldoutMotion | MotionFixture::DeviceHoldout => 2200,
        _ => 5600,
    };
    for n in 1..=frames {
        let now = u64::from(n) * 25_000;
        clock.store(now, Ordering::Relaxed);
        if n == 4 && device {
            owner
                .marker(csi_lab_core::ownership::Marker::Training {
                    command: 2,
                    status: 0,
                    wander: 0.0,
                    jitter: 0.0,
                })
                .unwrap();
        }
        if n == 4 && !device {
            recorder.push(Record::new(journal::CONTROL, now, &[2, 0, 0, 0]).unwrap());
        }
        if now == 100_000_000 {
            if device {
                owner
                    .marker(csi_lab_core::ownership::Marker::Label(2))
                    .unwrap();
            } else {
                recorder.push(Record::new(journal::LABEL, now, &[2]).unwrap());
            }
        }
        if now == 130_000_000 {
            if device {
                owner
                    .marker(csi_lab_core::ownership::Marker::Label(1))
                    .unwrap();
            } else {
                recorder.push(Record::new(journal::LABEL, now, &[1]).unwrap());
            }
        }
        let walking = if matches!(
            case,
            MotionFixture::HoldoutMotion | MotionFixture::DeviceHoldout
        ) {
            (50_000_000..82_000_000).contains(&now)
        } else {
            (100_000_000..130_000_000).contains(&now)
        };
        let values = if (walking && n.is_multiple_of(2)) || (!walking && now >= 50_000_000) {
            [1.0f32, 4.0, 2.0, 3.0]
        } else {
            [1.0f32, 2.0, 3.0, 4.0]
        };
        for part in 0..3 {
            let data = if part == 1 { &[][..] } else { &values[..] };
            let mut w = vec![n, part, 4, 1, 0, data.len() as u32, 0, 0];
            w.extend(data.iter().map(|v| v.to_bits()));
            recorder.push(Record::new(journal::MATRIX, now, &w).unwrap());
        }
        if matches!(
            case,
            MotionFixture::SourceFailure | MotionFixture::DeviceFault
        ) && n == 4100
        {
            runtime.fail(Rejection::SourceContention);
        }
        let feature = Feature {
            sequence: n,
            oldest_us: now - 5000,
            newest_us: now,
            templates: 1,
            training_complete: true,
            gain_available: true,
            wander: 0.01,
        };
        if device {
            let mut transient = values;
            let profile = csi_lab_core::motion_pipeline::Profile::copy(&transient).unwrap();
            ingress
                .observe_profile(feature, n, profile, |r| runtime.fail(r))
                .unwrap();
            transient.fill(f32::NAN);
            assert!(transient.iter().all(|v| v.is_nan()));
            owner.drain_ready().unwrap();
        } else {
            runtime
                .execute(
                    Operation::Observe(feature),
                    || now,
                    |mut e| {
                        e.words[23] = n;
                        recorder.push_model(e);
                    },
                )
                .unwrap();
        }
        if n.is_multiple_of(16) {
            recorder.drain(&mut wire).unwrap();
        }
    }
    let end = u64::from(frames) * 25_000;
    if matches!(case, MotionFixture::DeviceSilence) {
        clock.store(end + 1_000_001, Ordering::Relaxed);
        owner.operation(Operation::Poll).unwrap();
    }
    if matches!(case, MotionFixture::Silence) {
        runtime
            .execute(
                Operation::Poll,
                || end + 1_000_001,
                |event| recorder.push_model(event),
            )
            .unwrap();
    }
    let finish = end
        + if matches!(case, MotionFixture::Silence | MotionFixture::DeviceSilence) {
            1_000_002
        } else {
            1000
        };
    owner.close().unwrap();
    recorder.finish(finish, &mut wire).unwrap();
    assert_eq!(recorder.loss(), 0);
    wire
}

#[test]
fn full_motion_preview_requires_independent_holdout_and_keeps_source_faults() {
    for failure in [false, true] {
        let temp = Temp::new();
        let case = if failure {
            MotionFixture::SourceFailure
        } else {
            MotionFixture::Ordinary
        };
        let (path, r) = capture(&temp, &motion_policy_transport(case));
        assert!(r.status.success());
        let args = ["preview-motion", &path, "--case", "temporal-motion-v1"];
        let a = run(&args);
        let b = run(&args);
        assert!(a.status.success(), "{}", String::from_utf8_lossy(&a.stderr));
        assert_eq!(a.stdout, b.stdout);
        assert_eq!(a.stderr, b.stderr);
        let summary: Value = serde_json::from_slice(&a.stderr).unwrap();
        assert_eq!(summary["completed_policy_holdout"], true);
        assert_eq!(summary["physical_detector_admitted"], false);
        assert_eq!(summary["source_observations"], 5600);
        let text = String::from_utf8(a.stdout).unwrap();
        let rows: Vec<Value> = text
            .lines()
            .map(|s| serde_json::from_str(s).unwrap())
            .collect();
        assert!(rows
            .iter()
            .all(|r| r["evaluation"] == true && r["physical_detector_admitted"] == false));
        assert!(rows.iter().any(|r| r["snapshot"]["state"] == "moving"));
        let controls: Vec<_> = rows.iter().filter(|r| r["event"] == "control").collect();
        assert_eq!(controls.len(), 3);
        assert_eq!(controls[0]["control"], "begin_background");
        assert_eq!(controls[1]["control"], "begin_holdout");
        assert_eq!(controls[2]["control"], "accept");
        assert!(
            controls[2]["time_us"].as_u64().unwrap() - controls[1]["time_us"].as_u64().unwrap()
                >= 60_000_000
        );
        let entered = controls[1]["snapshot"]["enter_bits"].clone();
        assert!(rows
            .iter()
            .filter(|r| matches!(
                r["snapshot"]["phase"].as_str(),
                Some("holdout" | "ready" | "rejected")
            ))
            .all(|r| r["snapshot"]["enter_bits"] == entered));
        if failure {
            assert_eq!(summary["final"]["phase"], "rejected");
            assert_eq!(summary["final"]["state"], "unknown");
            assert_eq!(summary["final"]["fault"], "SourceUnavailable");
        } else {
            assert_eq!(summary["final"]["phase"], "ready");
            assert_eq!(summary["final"]["state"], "idle");
            assert_eq!(rows.last().unwrap()["event"], "end");
            assert!(
                summary["label_phase_bins_unknown_idle_moving"]["walking/ready"][2]
                    .as_u64()
                    .unwrap()
                    > 100
            );
        }
    }
}

#[test]
fn preview_emits_silent_expiry_and_keeps_the_rejecting_holdout_bin() {
    for case in [MotionFixture::Silence, MotionFixture::HoldoutMotion] {
        let temp = Temp::new();
        let (path, r) = capture(&temp, &motion_policy_transport(case));
        assert!(r.status.success());
        let r = run(&["preview-motion", &path, "--case", "temporal-motion-v1"]);
        assert!(r.status.success(), "{}", String::from_utf8_lossy(&r.stderr));
        let summary: Value = serde_json::from_slice(&r.stderr).unwrap();
        assert_eq!(summary["final"]["state"], "unknown");
        assert_eq!(summary["final"]["phase"], "rejected");
        let rows: Vec<Value> = String::from_utf8(r.stdout)
            .unwrap()
            .lines()
            .map(|s| serde_json::from_str(s).unwrap())
            .collect();
        if matches!(case, MotionFixture::Silence) {
            assert_eq!(summary["completed_policy_holdout"], true);
            assert_eq!(summary["final"]["fault"], "Expired");
            assert!(rows.iter().any(|r| r["event"] == "timer"
                && r["snapshot"]["state"] == "unknown"
                && r["snapshot"]["fault"] == "Expired"));
        } else {
            assert_eq!(summary["completed_policy_holdout"], false);
            assert_eq!(summary["final"]["fault"], "HoldoutMotion");
            assert_eq!(
                summary["completed_bins"].as_u64().unwrap(),
                128 + summary["final"]["phase_bins"].as_u64().unwrap()
            );
            assert_eq!(
                summary["label_phase_bins_unknown_idle_moving"]["empty/rejected"][0],
                1
            );
            assert!(rows
                .iter()
                .any(|r| r["snapshot"]["fault"] == "HoldoutMotion" && !r["bin"].is_null()));
        }
    }
}

#[test]
fn device_owner_motion_checkpoints_replay_exactly_and_tampering_is_rejected() {
    use csi_lab_core::journal;
    for case in [
        MotionFixture::Device,
        MotionFixture::DeviceFault,
        MotionFixture::DeviceSilence,
        MotionFixture::DeviceHoldout,
    ] {
        let temp = Temp::new();
        let wire = motion_policy_transport(case);
        let (path, r) = capture(&temp, &wire);
        assert!(r.status.success(), "{}", String::from_utf8_lossy(&r.stderr));
        let a = run(&["replay", &path]);
        let b = run(&["replay", &path]);
        assert!(a.status.success(), "{}", String::from_utf8_lossy(&a.stderr));
        assert_eq!(a.stdout, b.stdout);
        assert_eq!(a.stderr, b.stderr);
        let preview = run(&["preview-motion", &path, "--case", "temporal-motion-v1"]);
        assert!(
            preview.status.success(),
            "{}",
            String::from_utf8_lossy(&preview.stderr)
        );
        let summary: Value = serde_json::from_slice(&preview.stderr).unwrap();
        let expected = match case {
            MotionFixture::Device => "idle",
            _ => "unknown",
        };
        assert_eq!(summary["final"]["state"], expected);
        let mut records: Vec<_> = std::str::from_utf8(&wire)
            .unwrap()
            .lines()
            .map(|s| journal::Record::parse(s).unwrap())
            .collect();
        let motion = records
            .iter_mut()
            .find(|r| r.kind == journal::MOTION && r.words[1] == 1)
            .unwrap();
        motion.words[3] = 1; // CRC is rebuilt: this is a semantic checkpoint forgery.
        let mut changed = Vec::new();
        let mut chain = 0;
        for r in &mut records {
            if r.kind == journal::END {
                r.words[1] = chain;
            } else {
                chain = journal::chain(chain, r.checksum());
            }
            r.write(&mut changed).unwrap();
        }
        let other = Temp::new();
        let (_, r) = capture(&other, &changed);
        assert!(!r.status.success());
        assert!(
            String::from_utf8_lossy(&r.stderr).contains("motion live/replay checkpoint mismatch")
        );
    }
}

#[test]
fn complete_capture_exports_original_probe_inputs_and_retains_positive_and_fault_evidence() {
    for case in [MotionFixture::Device, MotionFixture::DeviceFault] {
        let temp = Temp::new();
        let (path, captured) = capture(&temp, &motion_policy_transport(case));
        assert!(captured.status.success());
        let input = temp.path("probe.bin");
        let exported = run(&["export-motion-probe", &path, &input]);
        assert!(
            exported.status.success(),
            "{}",
            String::from_utf8_lossy(&exported.stderr)
        );
        let a = run(&["run-motion-probe", &input]);
        let b = run(&["run-motion-probe", &input]);
        assert!(a.status.success(), "{}", String::from_utf8_lossy(&a.stderr));
        assert_eq!(a.stdout, b.stdout);
        let output = String::from_utf8(a.stdout).unwrap();
        assert!(output.contains("radio=0 virtual_time=1 physical_detector_admitted=0"));
        let checkpoints: Vec<Vec<u32>> = output
            .lines()
            .filter_map(|line| line.split_once(" bits=").map(|(_, bits)| bits))
            .map(|bits| {
                bits.as_bytes()
                    .chunks_exact(8)
                    .map(|w| u32::from_str_radix(std::str::from_utf8(w).unwrap(), 16).unwrap())
                    .collect()
            })
            .collect();
        assert!(checkpoints.iter().any(|w| w[3] == 2)); // Positive evidence excludes always idle.
        let last = checkpoints.last().unwrap();
        match case {
            MotionFixture::Device => {
                assert_eq!(last[2], 3);
                assert_eq!(last[3], 1);
            }
            _ => {
                assert_eq!(last[2], 4);
                assert_eq!(last[3], 0);
                assert_eq!(last[4], 11);
            }
        }
        let mut broken = fs::read(&input).unwrap();
        broken.pop();
        fs::write(&input, broken).unwrap();
        let failure = run(&["run-motion-probe", &input]);
        assert!(!failure.status.success());
        assert!(failure.stdout.is_empty());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(&input).unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
    }
}
