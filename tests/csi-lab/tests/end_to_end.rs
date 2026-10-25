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
