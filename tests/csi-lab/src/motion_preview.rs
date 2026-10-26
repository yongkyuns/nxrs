//! Development preview with a declared automatic schedule. Source labels and
//! native identities remain those of the original immutable capture. A preview
//! never constitutes physical detector admission or an independent benchmark.
use crate::{
    replay,
    signals::{self, Event, Unavailable},
    Result,
};
use csi_lab_core::motion::{self, Admission, Fault, Motion, Phase, Snapshot, State};
use serde_json::{json, Value};
use std::collections::BTreeMap;

fn phase(p: Phase) -> &'static str {
    match p {
        Phase::Uncalibrated => "uncalibrated",
        Phase::Background => "background",
        Phase::Holdout => "holdout",
        Phase::Ready => "ready",
        Phase::Rejected => "rejected",
    }
}
fn state(s: State) -> &'static str {
    match s {
        State::Unknown => "unknown",
        State::Idle => "idle",
        State::Moving => "moving",
    }
}
fn snapshot(s: Snapshot) -> Value {
    json!({"phase":phase(s.phase),"state":state(s.state),"fault":s.fault.map(|v|format!("{v:?}")),
        "phase_bins":s.phase_bins,"positive_bins":s.positive_bins,"quiet_bins":s.quiet_bins,
        "centre_bits":s.thresholds.map(|t|t.centre.to_bits()),"mad_bits":s.thresholds.map(|t|t.mad.to_bits()),
        "enter_bits":s.thresholds.map(|t|t.enter.to_bits()),"exit_bits":s.thresholds.map(|t|t.exit.to_bits())})
}
struct Preview {
    model: Motion,
    label: usize,
    label_since: u64,
    training_stopped: bool,
    accepted: bool,
    emit: bool,
    observations: u64,
    bins: u64,
    counts: BTreeMap<String, [u64; 3]>,
}
impl Preview {
    fn new(emit: bool) -> Self {
        Self {
            model: Motion::new(),
            label: 0,
            label_since: 0,
            training_stopped: false,
            accepted: false,
            emit,
            observations: 0,
            bins: 0,
            counts: BTreeMap::new(),
        }
    }
    fn control(&mut self, now: u64) -> Result<()> {
        let prior = self.model.snapshot();
        let (name, result) = match prior.phase {
            Phase::Uncalibrated if self.training_stopped && self.label == 1 => {
                ("begin_background", self.model.begin_background(now))
            }
            Phase::Background if prior.phase_bins >= motion::BACKGROUND_BINS as u32 => {
                ("begin_holdout", self.model.begin_holdout(now))
            }
            Phase::Holdout if prior.phase_bins >= motion::HOLDOUT_BINS => {
                ("accept", self.model.accept(now))
            }
            _ => return Ok(()),
        };
        match result {
            Ok(()) => {
                self.accepted |= name == "accept";
                if self.emit {
                    println!(
                        "{}",
                        json!({"case":"temporal-motion-v1","evaluation":true,"physical_detector_admitted":false,
                    "event":"control","control":name,"time_us":now,"snapshot":snapshot(self.model.snapshot())})
                    );
                }
            }
            Err(Admission::NoSource | Admission::InsufficientEvidence) => {}
            Err(Admission::Rejected(_)) => {
                if self.emit {
                    println!(
                        "{}",
                        json!({"case":"temporal-motion-v1","evaluation":true,"physical_detector_admitted":false,
                        "event":"control_rejected","control":name,"time_us":now,"snapshot":snapshot(self.model.snapshot())})
                    );
                }
            }
            Err(Admission::WrongPhase) => return Err("preview control phase mismatch".into()),
        }
        Ok(())
    }
    fn event(&mut self, event: Event) -> Result<()> {
        match event {
            Event::Clock(now) => {
                let before = self.model.snapshot();
                let _ = self.model.poll(now);
                let after = self.model.snapshot();
                if self.emit
                    && (before.phase, before.state, before.fault)
                        != (after.phase, after.state, after.fault)
                {
                    println!(
                        "{}",
                        json!({"case":"temporal-motion-v1","evaluation":true,"physical_detector_admitted":false,
                        "event":"timer","time_us":now,"snapshot":snapshot(after)})
                    );
                }
            }
            Event::End(now) => {
                let _ = self.model.poll(now);
                if self.emit {
                    println!(
                        "{}",
                        json!({"case":"temporal-motion-v1","evaluation":true,"physical_detector_admitted":false,
                    "event":"end","time_us":now,"snapshot":snapshot(self.model.snapshot())})
                    );
                }
            }
            Event::Label { time_us, label } => {
                self.label = label;
                self.label_since = time_us;
                if label != 1
                    && matches!(
                        self.model.snapshot().phase,
                        Phase::Background | Phase::Holdout
                    )
                {
                    self.model.reject(Fault::LabelViolation);
                }
            }
            Event::TrainingStop { time_us, status } => {
                if status == 0 {
                    self.training_stopped = true;
                    self.control(time_us)?;
                } else {
                    self.model.reject(Fault::SourceUnavailable);
                }
            }
            Event::Source {
                time_us,
                record_sequence,
                sample,
                unavailable,
            } => {
                self.observations += 1;
                let bin = if let Some(sample) = sample {
                    let prior = self.model.snapshot().last_bin.map(|v| v.id);
                    let result = self.model.observe(sample, time_us);
                    let current = self.model.snapshot().last_bin;
                    // The bin that causes holdout rejection is evidence too.
                    // Other source/timer failures do not create a new bin.
                    match result {
                        Ok(bin) => bin,
                        Err(_) => current.filter(|v| Some(v.id) != prior),
                    }
                } else {
                    match unavailable {
                        Some(Unavailable::Warmup) => {}
                        Some(Unavailable::Signal(fault)) => self.model.reject(Fault::Signal(fault)),
                        Some(Unavailable::RecordedSource(_) | Unavailable::Native) | None => {
                            self.model.reject(Fault::SourceUnavailable)
                        }
                    }
                    None
                };
                let current = self.model.snapshot();
                let mut binned = None;
                if let Some(bin) = bin {
                    self.bins += 1;
                    let label = if bin.oldest_us >= self.label_since {
                        self.label
                    } else {
                        0
                    };
                    let key = format!(
                        "{}/{}",
                        ["unknown", "empty", "walking", "seated"][label],
                        phase(current.phase)
                    );
                    let i = match current.state {
                        State::Unknown => 0,
                        State::Idle => 1,
                        State::Moving => 2,
                    };
                    self.counts.entry(key).or_default()[i] += 1;
                    binned = Some(
                        json!({"id":bin.id,"oldest_us":bin.oldest_us,"newest_us":bin.newest_us,
                        "sequence":bin.sequence,"window":bin.window,"samples":bin.samples,"score_bits":bin.score.to_bits(),
                        "label":(["unknown","empty","walking","seated"][label])}),
                    );
                }
                if self.emit {
                    println!(
                        "{}",
                        json!({"case":"temporal-motion-v1","evaluation":true,"physical_detector_admitted":false,
                    "event":"observation","record_sequence":record_sequence,"time_us":time_us,
                    "snapshot":snapshot(current),"bin":binned})
                    );
                }
                self.control(time_us)?;
            }
        }
        Ok(())
    }
    fn summary(&self) -> Value {
        json!({"case":"temporal-motion-v1","evaluation":true,"physical_detector_admitted":false,
            "original_capture_verified":true,"source_observations":self.observations,"completed_bins":self.bins,
            "completed_policy_holdout":self.accepted,"final":snapshot(self.model.snapshot()),
            "label_phase_bins_unknown_idle_moving":self.counts,
            "schedule":"Begin background after recorded successful vendor stop and explicit empty label; fit after 128 bins/32s, independent holdout 240 bins/60s. No old model calibration controls reused.",
            "scope":"Development preview. A previously inspected or truncated capture is not independent physical qualification; stationary occupancy, detection quality and runtime parity remain unqualified."})
    }
}
pub fn run(path: &str) -> Result<()> {
    replay(path, None, false)?;
    let mut validation = Preview::new(false);
    signals::visit(path, false, &mut |e| validation.event(e))?;
    let mut preview = Preview::new(true);
    signals::visit(path, false, &mut |e| preview.event(e))?;
    eprintln!("{}", preview.summary());
    Ok(())
}
