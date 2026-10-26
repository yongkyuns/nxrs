//! Development preview with a declared automatic schedule. Source labels and
//! native identities remain those of the original immutable capture. A preview
//! never constitutes physical detector admission or an independent benchmark.
use crate::{
    replay,
    signals::{self, Event},
    Result,
};
use csi_lab_core::{
    motion::{Phase, Snapshot, State},
    motion_trial::{Kind, Trial, Update},
};
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
    model: Trial,
    emit: bool,
    observations: u64,
    bins: u64,
    counts: BTreeMap<String, [u64; 3]>,
}
impl Preview {
    fn new(emit: bool) -> Self {
        Self {
            model: Trial::new(),
            emit,
            observations: 0,
            bins: 0,
            counts: BTreeMap::new(),
        }
    }
    fn event(&mut self, event: Event) -> Result<()> {
        let record_sequence = if let Event::Source {
            record_sequence, ..
        } = event
        {
            Some(record_sequence)
        } else {
            None
        };
        let mut updates = [None; 2];
        let mut count = 0;
        let Some(event) = signals::trial_event(event) else {
            return Ok(());
        };
        self.model.event(event, |update| {
            updates[count] = Some(update);
            count += 1;
        });
        for update in updates.into_iter().flatten() {
            self.output(update, record_sequence);
        }
        Ok(())
    }
    fn output(&mut self, update: Update, record_sequence: Option<u32>) {
        let Update {
            kind,
            time_us: now,
            label,
            bin,
            snapshot: current,
        } = update;
        let control = match kind {
            Kind::BeginBackground => Some("begin_background"),
            Kind::BeginHoldout => Some("begin_holdout"),
            Kind::Accept => Some("accept"),
            _ => None,
        };
        if let Some(control) = control {
            if self.emit {
                println!(
                    "{}",
                    json!({"case":"temporal-motion-v1","evaluation":true,"physical_detector_admitted":false,
                "event":"control","control":control,"time_us":now,"snapshot":snapshot(current)})
                );
            }
            return;
        }
        if matches!(kind, Kind::Label | Kind::TrainingStop) {
            return;
        }
        let mut binned = None;
        if kind == Kind::Source {
            self.observations += 1;
            if let Some(bin) = bin {
                self.bins += 1;
                let key = format!("{}/{}", label.name(), phase(current.phase));
                let i = match current.state {
                    State::Unknown => 0,
                    State::Idle => 1,
                    State::Moving => 2,
                };
                self.counts.entry(key).or_default()[i] += 1;
                binned = Some(
                    json!({"id":bin.id,"oldest_us":bin.oldest_us,"newest_us":bin.newest_us,
                    "sequence":bin.sequence,"window":bin.window,"samples":bin.samples,"score_bits":bin.score.to_bits(),"label":label.name()}),
                );
            }
        }
        if self.emit {
            let event = match kind {
                Kind::Source => "observation",
                Kind::Timer => "timer",
                Kind::End => "end",
                Kind::ControlRejected => "control_rejected",
                _ => unreachable!(),
            };
            let mut row = json!({"case":"temporal-motion-v1","evaluation":true,"physical_detector_admitted":false,
                "event":event,"time_us":now,"snapshot":snapshot(current)});
            if kind == Kind::Source {
                row["record_sequence"] = json!(record_sequence);
                row["bin"] = json!(binned);
            }
            println!("{row}");
        }
    }
    fn summary(&self) -> Value {
        json!({"case":"temporal-motion-v1","evaluation":true,"physical_detector_admitted":false,
            "original_capture_verified":true,"source_observations":self.observations,"completed_bins":self.bins,
            "completed_policy_holdout":self.model.completed_holdout(),"final":snapshot(self.model.snapshot()),
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
/// Verify the device's new model from original native output and source clock.
/// The historical model remains independently checked by the ordinary replay.
pub fn verify(path: &str) -> Result<u64> {
    use csi_lab_core::motion_wire::Checkpoint;
    use std::collections::VecDeque;
    let mut trial = Trial::new();
    let mut expected = VecDeque::<Checkpoint>::new();
    let mut checked = 0u64;
    signals::visit(path, false, &mut |event| {
        if let Event::Checkpoint(actual) = event {
            if expected.pop_front() != Some(actual) {
                return Err("motion live/replay checkpoint mismatch or order".into());
            }
            checked += 1;
        } else if let Some(event) = signals::trial_event(event) {
            trial.event(event, |update| {
                if update.kind != Kind::End {
                    expected.push_back(update.into());
                }
            });
            if expected.len() > 4 {
                return Err("missing motion checkpoint within bounded retention".into());
            }
        }
        Ok(())
    })?;
    if checked == 0 || !expected.is_empty() {
        return Err("missing/incomplete motion checkpoints".into());
    }
    Ok(checked)
}
