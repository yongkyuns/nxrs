//! CSI-only laboratory application. No serial/network/hardware is opened here;
//! a separately owned console supplies the firmware's versioned transport.
mod inputs;
mod motion_preview;
mod probe_input;
mod sampling;
mod signals;
#[path = "../target_probe.rs"]
mod target_probe;
use csi_lab_core::{
    calibration::{Calibration, Config, Feature},
    journal::{self, Integrity, Operation, Record},
    recorder::Recorder,
};
use serde_json::{json, Value};
use std::fs::{File, OpenOptions};
use std::io::{self, BufRead, BufReader, Write};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
fn private_file(path: &str) -> Result<File> {
    let mut opts = OpenOptions::new();
    opts.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    Ok(opts.open(path)?)
}
fn line(reader: &mut impl BufRead) -> Result<Option<String>> {
    let mut bytes = Vec::new();
    use std::io::Read;
    let n = reader.take(65537).read_until(b'\n', &mut bytes)?;
    if n == 0 {
        return Ok(None);
    }
    if n > 65536 || bytes.last() != Some(&b'\n') {
        return Err("oversize or unterminated line".into());
    }
    bytes.pop();
    if bytes.last() == Some(&b'\r') {
        bytes.pop();
    }
    Ok(Some(String::from_utf8(bytes)?))
}
fn manifest(path: &str) -> Result<Value> {
    if std::fs::metadata(path)?.len() > 32768 {
        return Err("manifest too large".into());
    }
    let m: Value = serde_json::from_reader(File::open(path)?)?;
    validate_manifest(&m)?;
    Ok(m)
}
fn validate_manifest(m: &Value) -> Result<()> {
    for key in ["source_commit", "firmware_sha256", "sdk_revision"] {
        let value = m[key].as_str().ok_or("missing provenance")?;
        let length = if key == "firmware_sha256" { 64 } else { 40 };
        if value.len() != length || !value.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(format!("invalid {key}").into());
        }
    }
    if m["scope"] != "calibration_and_numeric_inputs_v1" {
        return Err("unsupported capture scope".into());
    }
    if m["origin"] != "hardware" && m["origin"] != "synthetic_test" {
        return Err("explicit origin required".into());
    }
    Ok(())
}
fn record(manifest_path: &str, output: &str) -> Result<()> {
    let m = manifest(manifest_path)?;
    let mut out = private_file(output)?;
    writeln!(out, "{}", json!({"csi_lab":1,"manifest":m}))?;
    let mut reader = io::stdin().lock();
    let mut integrity = Integrity::default();
    let mut count = 0u64;
    while let Some(line) = line(&mut reader)? {
        if line.starts_with("PRESENCE_FAIL") {
            return Err("firmware failed; capture incomplete".into());
        }
        if !line.starts_with("CSI_LAB") {
            continue;
        }
        let r = Record::parse(&line)?;
        integrity.accept(&r)?;
        r.write(&mut out)?;
        count += 1;
    }
    integrity.finish()?;
    out.sync_all()?;
    replay(output, None, false)?;
    eprintln!("complete capture: {count} records; private file created");
    Ok(())
}
fn header(reader: &mut impl BufRead) -> Result<Value> {
    let h: Value = serde_json::from_str(&line(reader)?.ok_or("missing manifest")?)?;
    if h["csi_lab"] != 1 {
        return Err("container version".into());
    }
    validate_manifest(&h["manifest"])?;
    Ok(h)
}
fn replay(path: &str, sensitivity: Option<f32>, emit: bool) -> Result<Value> {
    let mut reader = BufReader::new(File::open(path)?);
    let h = header(&mut reader)?;
    let mut integrity = Integrity::default();
    let mut model = None;
    let mut records = 0u64;
    let mut counts = [[0u64; 3]; 4];
    let mut label = 0usize;
    let mut inputs = inputs::Inputs::default();
    let mut raw = 0u64;
    let mut matrices = 0u64;
    let mut features = 0u64;
    let mut source_seen = false;
    let mut motion_checkpoints = 0u64;
    let mut motion_declared = false;
    while let Some(line) = line(&mut reader)? {
        let r = Record::parse(&line)?;
        integrity.accept(&r)?;
        records += 1;
        if h["manifest"]["origin"] == "hardware"
            && matches!(
                r.kind,
                journal::RAW | journal::GAIN | journal::MATRIX | journal::WANDER | journal::MODEL
            )
            && !source_seen
        {
            return Err("missing selected source identity".into());
        }
        inputs.accept(&r)?;
        if records > 2_000_000 {
            return Err("capture record bound".into());
        }
        match r.kind {
            journal::SESSION => {
                let mut c = journal::config(&r)?;
                if let Some(s) = sensitivity {
                    c.sensitivity = s;
                }
                model = Some(Calibration::new(c).ok_or("invalid evaluation config")?);
            }
            journal::MODEL => {
                let s = journal::replay_model(
                    model.as_mut().ok_or("model before session")?,
                    &r,
                    sensitivity.is_none(),
                )?;
                if r.words[0] == 4 {
                    features += 1;
                    counts[label][s.state as usize] += 1;
                }
                if emit {
                    println!(
                        "{}",
                        json!({"sequence":r.sequence,"time_us":r.time_us,"phase":s.phase.name(),"state":s.state.name(),"rejection":s.rejection.name(),"features":s.features,"average_bits":s.average.to_bits(),"enter_bits":s.enter.to_bits(),"exit_bits":s.exit.to_bits(),"label":(["unknown","empty","walking","seated"][label]),"evaluation":sensitivity.is_some()})
                    );
                }
            }
            journal::LABEL => {
                if r.len != 1 || r.words[0] > 3 {
                    return Err("label schema".into());
                }
                label = r.words[0] as usize;
            }
            journal::RAW => raw += 1,
            journal::MATRIX => matrices += 1,
            journal::CONTROL => {
                if r.len != 4 || !(1..=4).contains(&r.words[0]) {
                    return Err("training control schema".into());
                }
                if r.words[0] == 3 {
                    if source_seen
                        || !(1..=14).contains(&r.words[1])
                        || r.words[2] & 1 != 0
                        || (r.words[2] == 0 && r.words[3] == 0)
                        || r.words[3] > 0xffff
                    {
                        return Err("source identity schema".into());
                    }
                    source_seen = true;
                }
                if r.words[0] == 4 {
                    if motion_declared
                        || motion_checkpoints != 0
                        || features != 0
                        || r.data() != [4, 1, 16, 0]
                    {
                        return Err("motion case declaration".into());
                    }
                    motion_declared = true;
                }
            }
            journal::SAMPLING => csi_lab_core::sampling::validate(&r)?,
            journal::GAIN | journal::WANDER | journal::END => {}
            journal::MOTION => {
                csi_lab_core::motion_wire::validate(r.data())?;
                motion_checkpoints += 1;
            }
            _ => return Err("unknown record kind".into()),
        }
    }
    integrity.finish()?;
    inputs.finish()?;
    if motion_declared != (motion_checkpoints > 0) {
        return Err("motion case/checkpoints missing".into());
    }
    if motion_checkpoints > 0 && motion_preview::verify(path)? != motion_checkpoints {
        return Err("motion checkpoint count mismatch".into());
    }
    if h["manifest"]["origin"] == "hardware" && !source_seen {
        return Err("missing selected source identity".into());
    }
    Ok(
        json!({"records":records,"features":features,"raw_records":raw,"matrix_chunks":matrices,"origin":h["manifest"]["origin"],"verified_live_checkpoints":sensitivity.is_none(),"evaluation_sensitivity":sensitivity,"label_feature_counts_unknown_clear_presence":counts,"native_vendor_engine_replayed":false}),
    )
}
fn export(path: &str, output: &str) -> Result<()> {
    replay(path, None, false)?;
    let mut reader = BufReader::new(File::open(path)?);
    let h = header(&mut reader)?;
    let mut out = private_file(output)?;
    writeln!(
        out,
        "{}",
        json!({"csi_lab_kernels":1,"manifest":h["manifest"]})
    )?;
    let mut inputs = inputs::Inputs::default();
    while let Some(line) = line(&mut reader)? {
        let r = Record::parse(&line)?;
        if let Some(call) = inputs.accept(&r)? {
            writeln!(out, "{call}")?;
        }
    }
    inputs.finish()?;
    out.sync_all()?;
    Ok(())
}
fn fixture() -> Result<()> {
    let c = Config {
        minimum_features: 25,
        minimum_duration_us: 1000,
        ..Config::EMPTY_HOLDOUT
    };
    let runtime = csi_lab_core::calibration::Runtime::new();
    assert!(runtime.prepare(c));
    let recorder = Recorder::new();
    assert!(recorder.prepare(c, 256));
    let mut out = io::stdout().lock();
    let mut n = 0u32;
    let send = |op, now| {
        runtime
            .execute(op, || now, |r| recorder.push_model(r))
            .unwrap();
    };
    // Known synthetic Pearson identity, used by host export + native QEMU smoke.
    let bits = [
        1.0f32.to_bits(),
        2.0f32.to_bits(),
        3.0f32.to_bits(),
        4.0f32.to_bits(),
    ];
    for part in 0..2 {
        let mut words = vec![1, part, 4, 0, 0, 4, 0, 0];
        words.extend_from_slice(&bits);
        recorder.push(Record::new(journal::WANDER, 0, &words)?);
    }
    recorder.push(Record::new(
        journal::WANDER,
        0,
        &[1, 2, 4, 0, 0, 1, 0, 0, 0],
    )?);
    recorder.push(Record::new(journal::LABEL, 0, &[1])?);
    send(Operation::Start(10), 0);
    for phase in 0..4 {
        if phase == 1 {
            send(Operation::Validate, u64::from(n) * 1000);
        }
        if phase == 2 {
            send(Operation::Accept, u64::from(n) * 1000);
            recorder.push(Record::new(journal::LABEL, u64::from(n) * 1000, &[2])?);
        }
        if phase == 3 {
            recorder.push(Record::new(journal::LABEL, u64::from(n) * 1000, &[1])?);
        }
        for _ in 0..60 {
            n += 1;
            let now = u64::from(n) * 1000;
            send(
                Operation::Observe(Feature {
                    sequence: n,
                    oldest_us: now,
                    newest_us: now,
                    templates: 10,
                    training_complete: true,
                    gain_available: true,
                    wander: if phase == 2 { 0.02 } else { 1.0 / 1024.0 },
                }),
                now,
            );
        }
        recorder.drain(&mut out)?;
    }
    send(Operation::Poll, u64::from(n) * 1000 + 1_000_001);
    recorder.finish(u64::from(n) * 1000 + 1_000_002, &mut out)?;
    Ok(())
}
fn run() -> Result<()> {
    let a: Vec<_> = std::env::args().skip(1).collect();
    match a.iter().map(String::as_str).collect::<Vec<_>>().as_slice() {
        ["record",manifest,output]=>record(manifest,output),
        ["replay",path]=> { let summary=replay(path,None,false)?;replay(path,None,true)?; eprintln!("{summary}");Ok(()) },
        ["evaluate",path,"--sensitivity",s]=> { let s:f32=s.parse()?; replay(path,None,false)?; let summary=replay(path,Some(s),true)?;eprintln!("{summary}");Ok(()) },
        ["fixture"]=>fixture(),
        ["export-kernels",path,output]=>export(path,output),
        ["sampling",path]=>sampling::run(path),
        ["signals",path,"--case","recent-three-v1"]=>signals::run(path),
        ["verify-distance",path]=>signals::verify_distance(path),
        ["preview-motion",path,"--case","temporal-motion-v1"]=>motion_preview::run(path),
        ["export-motion-probe",path,output]=>probe_input::export(path,output),
        ["run-motion-probe",path]=>{if std::fs::metadata(path)?.len()>target_probe::MAX_BYTES as u64 {return Err("probe input byte bound".into());}let bytes=std::fs::read(path)?;target_probe::run(&bytes,&mut io::stdout().lock())?;Ok(())},
        _=>Err("usage: csi-lab record MANIFEST.json OUTPUT.csilab < console.log | replay FILE | evaluate FILE --sensitivity 0.25 | signals FILE --case recent-three-v1 | preview-motion FILE --case temporal-motion-v1 | export-motion-probe FILE OUTPUT | run-motion-probe FILE | verify-distance FILE | sampling FILE | export-kernels FILE OUTPUT | fixture (synthetic test transport only)".into()),
    }
}
fn main() {
    if let Err(e) = run() {
        eprintln!("CSI lab failed: {e}");
        std::process::exit(1);
    }
}
