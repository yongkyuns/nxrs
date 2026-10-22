use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::channel;
use std::time::Duration;

struct Guard(Child);

impl Drop for Guard {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn start(mode: &str) -> Guard {
    Guard(
        Command::new(env!("CARGO_BIN_EXE_nxrs-csi-presence-eval"))
            .args(["--peer", "02:00:00:00:00:01", "--mode", mode])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap(),
    )
}

fn sample(timestamp: u64, present: bool) -> String {
    let value = json!({"type":"sample", "ts_ms":timestamp, "mac":"02:00:00:00:00:01",
        "motion_status":false, "presence_someone_status":present,
        "presence_ready":true, "train_thresholds_valid":true,
        "train_status_name":"COMPLETE", "init_stage_name":"STABLE"});
    format!("HMS:{value}\n")
}

fn replay(input: &str) -> (bool, Vec<Value>) {
    let mut child = start("replay");
    let mut writer = child.0.stdin.take().unwrap();
    writer.write_all(input.as_bytes()).unwrap();
    drop(writer);
    let mut bytes = String::new();
    std::io::Read::read_to_string(&mut child.0.stdout.take().unwrap(), &mut bytes).unwrap();
    let success = child.0.wait().unwrap().success();
    (
        success,
        bytes
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect(),
    )
}

#[test]
fn protocol_fixture_replays_calibration_presence_motion_loss_and_reboot() {
    let (success, values) = replay(include_str!("../fixtures/lifecycle.log"));
    assert!(success);
    let states: Vec<_> = values
        .iter()
        .map(|value| value["state"].as_str().unwrap())
        .collect();
    assert_eq!(
        states,
        [
            "unknown",
            "unknown",
            "unknown",
            "not_detected",
            "presence_hint",
            "motion_hint",
            "unknown",
            "not_detected",
            "unknown",
            "presence_hint",
            "unknown"
        ]
    );
    assert_eq!(values[1]["reason"], "calibration_required");
    assert_eq!(values[2]["reason"], "calibrating");
    for value in &values {
        assert_eq!(value["clock"], "replay");
        assert_eq!(value["experimental"], true);
        assert_eq!(value["advisory_only"], true);
        assert_eq!(value["rf_sample_freshness"], "not_exported_by_demo");
    }
}

#[test]
fn replay_discontinuity_fails_and_invalidates_previous_presence() {
    let input = sample(0, true) + &sample(50, true) + &sample(100, true) + &sample(90, true);
    let (success, values) = replay(&input);
    assert!(!success);
    assert!(values.iter().any(|value| value["state"] == "presence_hint"));
    assert_eq!(values.last().unwrap()["state"], "unknown");
}

#[test]
fn malformed_input_fails_and_invalidates_previous_presence() {
    let input = sample(0, true) + &sample(50, true) + &sample(100, true) + "HMS:{broken}\n";
    let (success, values) = replay(&input);
    assert!(!success);
    assert_eq!(values.last().unwrap()["state"], "unknown");
}

#[test]
fn silence_expires_while_input_remains_open() {
    let mut child = start("observer");
    let mut writer = child.0.stdin.take().unwrap();
    let stdout = child.0.stdout.take().unwrap();
    let (sender, receiver) = channel();
    let reader = std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            let value: Value = serde_json::from_str(&line.unwrap()).unwrap();
            if sender.send(value).is_err() {
                break;
            }
        }
    });
    for timestamp in [0, 50, 100] {
        writer
            .write_all(sample(timestamp, true).as_bytes())
            .unwrap();
        writer.flush().unwrap();
        std::thread::sleep(Duration::from_millis(100));
    }
    let mut detected = false;
    loop {
        let value = receiver.recv_timeout(Duration::from_secs(5)).unwrap();
        detected |= value["state"] == "presence_hint";
        if value["reason"] == "report_stream_lost" {
            assert!(detected);
            assert_eq!(value["clock"], "observer");
            break;
        }
    }
    // EOF was deliberately withheld until the deadline had fired.
    drop(writer);
    assert!(child.0.wait().unwrap().success());
    reader.join().unwrap();
}

#[test]
fn mode_must_be_explicit_and_no_hardware_is_opened() {
    let output = Command::new(env!("CARGO_BIN_EXE_nxrs-csi-presence-eval"))
        .args(["--peer", "02:00:00:00:00:01"])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(output.stdout.is_empty());
}
