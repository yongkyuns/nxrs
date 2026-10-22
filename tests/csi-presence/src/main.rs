//! Evaluation observer only: stdin/stdout, no serial port or network access.
#![forbid(unsafe_code)]

use nxrs_csi_presence_eval::{parse_line, read_line, Peer, Report};
use presence_service::{Config, Evidence, State, Tracker, UnknownReason};
use std::io::{BufReader, Write};
use std::sync::mpsc::{sync_channel, RecvTimeoutError};
use std::time::{Duration, Instant};

#[derive(Clone, Copy, PartialEq, Eq)]
enum Mode {
    Replay,
    Observer,
}

fn emit(state: State, now_ms: u64, mode: Mode) -> Result<(), String> {
    let (name, reason) = match state {
        State::Unknown(reason) => (
            "unknown",
            Some(match reason {
                UnknownReason::WaitingForSamples => "waiting_for_samples",
                UnknownReason::CalibrationRequired => "calibration_required",
                UnknownReason::Calibrating => "calibrating",
                UnknownReason::SignalLost => "report_stream_lost",
            }),
        ),
        State::NotDetected => ("not_detected", None),
        State::Detected(Evidence::Motion) => ("motion_hint", None),
        State::Detected(Evidence::Presence) => ("presence_hint", None),
    };
    let value = serde_json::json!({
        "schema":1, "experimental":true, "advisory_only":true,
        "source":"espressif_wifi_sensing_demo", "time_ms":now_ms,
        "clock":if mode == Mode::Replay {"replay"} else {"observer"},
        "rf_sample_freshness":"not_exported_by_demo",
        "state":name, "reason":reason,
    });
    let mut out = std::io::stdout().lock();
    writeln!(out, "{value}")
        .and_then(|_| out.flush())
        .map_err(|_| "cannot write output".to_owned())
}

fn run() -> Result<(), String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.len() != 4 || args[0] != "--peer" || args[2] != "--mode" {
        return Err("usage: nxrs-csi-presence-eval --peer AA:BB:CC:DD:EE:FF --mode replay|observer < input.log".to_owned());
    }
    let peer = Peer::parse(&args[1])?;
    let mode = match args[3].as_str() {
        "replay" => Mode::Replay,
        "observer" => Mode::Observer,
        _ => return Err("mode must explicitly be replay or observer".to_owned()),
    };
    let started = Instant::now();
    // One bounded fixture reader; the reusable service owns no worker. The
    // main owner still expires a report deadline when stdin stops producing.
    let (sender, receiver) = sync_channel(8);
    std::thread::spawn(move || {
        let mut reader = BufReader::with_capacity(2_048, std::io::stdin());
        loop {
            let result = match read_line(&mut reader) {
                Ok(Some(line)) => parse_line(&line, peer),
                Ok(None) => break,
                Err(_) => Err("invalid or unreadable input line"),
            };
            if matches!(result, Ok(None)) {
                continue;
            }
            let terminal = result.is_err();
            if sender.send(result).is_err() || terminal {
                break;
            }
        }
    });

    let mut tracker = Tracker::new(Config::default()).map_err(|error| format!("{error:?}"))?;
    let mut last_output = tracker.state();
    let mut replay_now = 0u64;
    let mut last_source = None;
    emit(last_output, 0, mode)?;
    loop {
        let message = receiver.recv_timeout(Duration::from_millis(50));
        let mut now = if mode == Mode::Replay {
            replay_now
        } else {
            started.elapsed().as_millis().min(u64::MAX as u128) as u64
        };
        let mut finished = false;
        let update = match message {
            Ok(Ok(Some(Report::Hello))) => {
                last_source = None;
                tracker.begin_session(now)
            }
            Ok(Ok(Some(Report::Sample(sample)))) => {
                if mode == Mode::Replay {
                    if let Some(last) = last_source {
                        let next = sample
                            .source_timestamp_ms
                            .checked_sub(last)
                            .filter(|delta| *delta > 0)
                            .and_then(|delta| replay_now.checked_add(delta));
                        let Some(next) = next else {
                            tracker
                                .source_lost(now)
                                .map_err(|error| format!("{error:?}"))?;
                            emit(tracker.state(), now, mode)?;
                            return Err("source clock did not advance or overflowed; a new session requires HELLO".to_owned());
                        };
                        replay_now = next;
                    }
                    now = replay_now;
                    last_source = Some(sample.source_timestamp_ms);
                }
                tracker.on_sample(sample, now)
            }
            Ok(Ok(None)) => continue,
            Ok(Err(error)) => {
                tracker
                    .source_lost(now)
                    .map_err(|error| format!("{error:?}"))?;
                emit(tracker.state(), now, mode)?;
                return Err(error.to_owned());
            }
            Err(RecvTimeoutError::Timeout) => tracker.poll(now),
            Err(RecvTimeoutError::Disconnected) => {
                finished = true;
                tracker.source_lost(now)
            }
        };
        let state = match update {
            Ok(state) => state,
            Err(error) => {
                tracker
                    .source_lost(now)
                    .map_err(|error| format!("{error:?}"))?;
                emit(tracker.state(), now, mode)?;
                return Err(format!("invalid observation: {error:?}"));
            }
        };
        if state != last_output {
            emit(state, now, mode)?;
            last_output = state;
        }
        if finished {
            break;
        }
    }
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("CSI evaluation stopped: {error}");
        std::process::exit(1);
    }
}
