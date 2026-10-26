//! Explicit counterfactual signals from a fully validated immutable capture.
//! This does not admit a detector or replace any recorded live checkpoint.
use crate::{header, inputs, line, replay, Result};
use csi_lab_core::{journal, temporal};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, VecDeque},
    fs::File,
    io::BufReader,
};

const PENDING_WINDOWS: usize = 2048;
#[derive(Clone, Copy)]
pub(crate) enum Unavailable {
    Warmup,
    RecordedSource(u32),
    Native,
    Signal(temporal::Fault),
}
impl Unavailable {
    fn description(self) -> String {
        match self {
            Self::Warmup => "warmup".to_owned(),
            Self::RecordedSource(code) => format!("recorded_external_fault:{code}"),
            Self::Native => "native_error".to_owned(),
            Self::Signal(fault) => format!("signal_fault:{fault:?}"),
        }
    }
}
#[derive(Clone, Copy)]
pub(crate) enum Event {
    Clock(u64),
    Label {
        time_us: u64,
        label: usize,
    },
    TrainingStop {
        time_us: u64,
        status: i32,
    },
    Source {
        time_us: u64,
        record_sequence: u32,
        sample: Option<temporal::Sample>,
        unavailable: Option<Unavailable>,
    },
    End(u64),
}
struct Native {
    status: i64,
    values: Vec<f32>,
}
pub(crate) fn visit(
    path: &str,
    emit: bool,
    visitor: &mut impl FnMut(Event) -> Result<()>,
) -> Result<Value> {
    let mut reader = BufReader::new(File::open(path)?);
    let _ = header(&mut reader)?;
    let mut inputs = inputs::Inputs::default();
    let mut integrity = journal::Integrity::default();
    let mut pending = BTreeMap::<u32, Native>::new();
    let mut order = VecDeque::new();
    let mut signal = None;
    let mut label = 0usize;
    let mut external_fault = None;
    let mut numerical_fault = false;
    let mut features = 0u64;
    let mut available = 0u64;
    while let Some(line) = line(&mut reader)? {
        let r = journal::Record::parse(&line)?;
        integrity.accept(&r)?;
        if let Some(c) = inputs.accept(&r)? {
            if c["kind"] == "motion" {
                let id = c["id"].as_u64().ok_or("native window")? as u32;
                let values = c["output_bits"]
                    .as_array()
                    .ok_or("native output")?
                    .iter()
                    .map(|v| Ok(f32::from_bits(v.as_u64().ok_or("output word")? as u32)))
                    .collect::<Result<Vec<_>>>()?;
                pending.insert(
                    id,
                    Native {
                        status: c["status"].as_i64().ok_or("native status")?,
                        values,
                    },
                );
                order.push_back(id);
                if order.len() > PENDING_WINDOWS {
                    pending.remove(&order.pop_front().unwrap());
                }
            }
        }
        match r.kind {
            journal::SESSION => {
                signal = temporal::Temporal::new(journal::config(&r)?.freshness_us);
            }
            journal::LABEL => {
                label = r.words[0] as usize;
                visitor(Event::Label {
                    time_us: r.time_us,
                    label,
                })?;
            }
            journal::CONTROL if r.words[0] == 2 => {
                visitor(Event::TrainingStop {
                    time_us: r.time_us,
                    status: r.words[1] as i32,
                })?;
            }
            journal::END => visitor(Event::End(r.time_us))?,
            journal::MODEL => {
                let p = signal.as_mut().ok_or("model before session")?;
                external_fault =
                    external_fault.or([r.words[1], r.words[2]].into_iter().find(|v| *v != 0));
                let freshness = p.check_fresh(r.time_us);
                visitor(Event::Clock(r.time_us))?;
                if r.words[0] != 4 {
                    continue;
                }
                let window = r.words[23];
                if window == 0 {
                    return Err("signal evaluation requires exact native window identity".into());
                }
                let native = pending.remove(&window).ok_or("native window missing or outside bounded retention; no identity guess permitted")?;
                numerical_fault |= native.status != 0;
                let mut sample = None;
                let reason = if let Some(code) = external_fault {
                    Some(Unavailable::RecordedSource(code))
                } else if numerical_fault {
                    Some(Unavailable::Native)
                } else if let Err(fault) = freshness {
                    Some(Unavailable::Signal(fault))
                } else {
                    match p.observe(
                        temporal::Input {
                            sequence: r.words[13],
                            window,
                            oldest_us: u64::from(r.words[14]) | u64::from(r.words[15]) << 32,
                            newest_us: u64::from(r.words[16]) | u64::from(r.words[17]) << 32,
                            values: &native.values,
                        },
                        r.time_us,
                    ) {
                        Ok(Some(value)) => {
                            sample = Some(value);
                            None
                        }
                        Ok(None) => Some(Unavailable::Warmup),
                        Err(fault) => Some(Unavailable::Signal(fault)),
                    }
                };
                features += 1;
                available += u64::from(sample.is_some());
                visitor(Event::Source {
                    time_us: r.time_us,
                    record_sequence: r.sequence,
                    sample,
                    unavailable: reason,
                })?;
                if emit {
                    println!(
                        "{}",
                        json!({
                            "csi_signal":1,"case":"recent-three-v1","evaluation":true,
                            "detector_admitted":false,"original_capture_verified":true,
                            "record_sequence":r.sequence,"time_us":r.time_us,
                            "source_sequence":r.words[13],"window":window,
                        "label":(["unknown","empty","walking","seated"][label]),
                        "original_phase":(["uncalibrated","background","holdout","ready","rejected"][r.words[4] as usize]),
                            "absolute_wander_bits":r.words[21],
                            "available":sample.is_some(),"reason":reason.map(Unavailable::description),
                            "score_bits":sample.map(|v|v.value.to_bits()),
                            "oldest_us":sample.map(|v|v.oldest_us),
                            "newest_us":sample.map(|v|v.newest_us),
                        })
                    );
                }
            }
            _ => {}
        }
    }
    inputs.finish()?;
    integrity.finish()?;
    Ok(
        json!({"case":"recent-three-v1","evaluation":true,"detector_admitted":false,
        "features":features,"available_signals":available,"unavailable_signals":features-available,
        "original_capture_verified":true,"pending_window_limit":PENDING_WINDOWS,
        "scope":"Derived temporal feature only. Calibration, holdout and alert policy are not evaluated or admitted."}),
    )
}

pub fn run(path: &str) -> Result<()> {
    replay(path, None, false)?;
    // Refuse corrupt captures, missing identities and unsupported source joins
    // before emitting even a prefix of counterfactual feature observations.
    visit(path, false, &mut |_| Ok(()))?;
    let summary = visit(path, true, &mut |_| Ok(()))?;
    eprintln!("{summary}");
    Ok(())
}

pub fn verify_distance(path: &str) -> Result<()> {
    replay(path, None, false)?;
    let mut reader = BufReader::new(File::open(path)?);
    let _ = header(&mut reader)?;
    let mut integrity = journal::Integrity::default();
    let mut inputs = inputs::Inputs::default();
    let mut calls = 0u64;
    while let Some(line) = line(&mut reader)? {
        let r = journal::Record::parse(&line)?;
        integrity.accept(&r)?;
        let Some(c) = inputs.accept(&r)? else {
            continue;
        };
        if c["kind"] != "wander" {
            continue;
        }
        let vector = |field: &str| -> Result<Vec<f32>> {
            c[field]
                .as_array()
                .ok_or("vector words")?
                .iter()
                .map(|v| Ok(f32::from_bits(v.as_u64().ok_or("float word")? as u32)))
                .collect()
        };
        let a = vector("input0_bits")?;
        let b = vector("input1_bits")?;
        let predicted = temporal::distance(&a, &b).map_err(|_| {
            "recorded comparison is outside candidate numerical support; unavailable"
        })?;
        if c["output_bits"][0].as_u64() != Some(u64::from(predicted.to_bits())) {
            return Err("Rust/native Pearson result bits differ".into());
        }
        calls += 1;
    }
    integrity.finish()?;
    inputs.finish()?;
    if calls == 0 {
        return Err("no recorded comparisons to verify".into());
    }
    println!(
        "{}",
        json!({"original_capture_verified":true,"native_comparisons":calls,"all_result_bits_exact":true,
        "scope":"Rust Pearson arithmetic against every recorded native comparison. Not radio, detector or execution-cost qualification."})
    );
    Ok(())
}
