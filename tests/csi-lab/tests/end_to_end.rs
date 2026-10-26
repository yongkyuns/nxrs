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
