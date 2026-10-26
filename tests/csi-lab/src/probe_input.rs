//! Export only original inputs, not recorded model answers, into the target probe.
use crate::{header, inputs, line, private_file, replay, signals, target_probe, Result};
use csi_lab_core::{journal, motion, motion_trial::Trial, motion_wire::Checkpoint};
use std::{
    collections::{BTreeSet, VecDeque},
    fs::File,
    io::{BufReader, Write},
};

fn words(time: u64) -> [u32; 2] {
    [time as u32, (time >> 32) as u32]
}
pub fn export(path: &str, output: &str) -> Result<()> {
    replay(path, None, false)?;
    let mut bytes = target_probe::MAGIC.to_vec();
    target_probe::append(&mut bytes, target_probe::BEGIN, &[1])?;
    let mut ended = None;
    let mut trial = Trial::new();
    let mut expected = Vec::<Checkpoint>::new();
    signals::visit(path, false, &mut |event| {
        if let Some(event) = signals::trial_event(event) {
            trial.event(event, |u| expected.push(u.into()));
        }
        match event {
            signals::Event::Clock {
                time_us,
                unavailable,
            } => {
                let mut w = words(time_us).to_vec();
                w.push(u32::from(matches!(
                    unavailable,
                    Some(motion::Fault::SourceUnavailable)
                )));
                target_probe::append(&mut bytes, target_probe::CLOCK, &w)?;
            }
            signals::Event::Label { time_us, label } => {
                let mut w = words(time_us).to_vec();
                w.push(label as u32);
                target_probe::append(&mut bytes, target_probe::LABEL, &w)?;
            }
            signals::Event::TrainingStop { time_us, status } => {
                let mut w = words(time_us).to_vec();
                w.push(u32::from(status == 0));
                target_probe::append(&mut bytes, target_probe::TRAIN_STOP, &w)?;
            }
            signals::Event::NativeSource {
                time_us,
                feature: f,
                window,
                values,
                columns,
                unavailable,
            } => {
                if columns > csi_lab_core::motion_pipeline::COLUMNS {
                    return Err("profile outside named target probe case".into());
                }
                let mut w = words(time_us).to_vec();
                w.extend([f.sequence, window]);
                w.extend(words(f.oldest_us));
                w.extend(words(f.newest_us));
                w.extend([
                    u32::from(f.training_complete),
                    u32::from(f.gain_available),
                    columns as u32,
                    u32::from(unavailable),
                ]);
                w.extend(values[..columns].iter().map(|v| v.to_bits()));
                target_probe::append(&mut bytes, target_probe::SOURCE, &w)?;
            }
            signals::Event::End(time) => ended = Some(time),
            _ => {}
        }
        if bytes.len() > target_probe::MAX_BYTES {
            return Err("probe export exceeds byte bound".into());
        }
        Ok(())
    })?;
    // Declared sampling independent of outcome: first/last 16 SDK comparisons.
    let mut reader = BufReader::new(File::open(path)?);
    header(&mut reader)?;
    let mut native = inputs::Inputs::default();
    let mut first = Vec::new();
    let mut last = VecDeque::new();
    while let Some(line) = line(&mut reader)? {
        let r = journal::Record::parse(&line)?;
        if r.kind == journal::SESSION && journal::config(&r)?.freshness_us != motion::FRESHNESS_US {
            return Err("probe requires declared source freshness".into());
        }
        let Some(c) = native.accept(&r)? else {
            continue;
        };
        if c["kind"] != "wander" {
            continue;
        }
        if first.len() < 16 {
            first.push(c.clone());
        }
        last.push_back(c);
        if last.len() > 16 {
            last.pop_front();
        }
    }
    native.finish()?;
    let mut seen = BTreeSet::new();
    for c in first.iter().chain(last.iter()) {
        if !seen.insert(c["id"].as_u64().ok_or("native comparison identity")?) {
            continue;
        }
        let mut w = vec![
            c["cols"].as_u64().ok_or("probe comparison columns")? as u32,
            c["output_bits"][0]
                .as_u64()
                .ok_or("probe comparison output")? as u32,
        ];
        for field in ["input0_bits", "input1_bits"] {
            for word in c[field].as_array().ok_or("probe comparison input")? {
                w.push(word.as_u64().ok_or("probe input word")? as u32);
            }
        }
        target_probe::append(&mut bytes, target_probe::DISTANCE, &w)?;
    }
    target_probe::append(
        &mut bytes,
        target_probe::END,
        &words(ended.ok_or("probe requires END")?),
    )?;
    let mut actual = Vec::new();
    target_probe::execute(&bytes, |_, c| actual.push(c), |_, _| {})?;
    if actual != expected {
        return Err("probe inputs differ from validated policy preview".into());
    }
    let mut out = private_file(output)?;
    out.write_all(&bytes)?;
    out.sync_all()?;
    Ok(())
}
