//! Full, unchanged Espressif reference engine; not a production Nxrs provider.
#[path = "../../idf60/src/lib.rs"]
#[allow(dead_code)]
mod sdk;
pub use sdk::{Frame, Metadata, Observation, Queue, QUEUE_SIZE};
mod calibration;
mod capture;
mod numerical;
mod trace;
use std::ffi::c_void;
use std::io::Write;
use std::time::{Duration, Instant};

#[repr(C)]
#[derive(Default)]
struct Diagnostic {
    wander: f32,
    jitter: f32,
    average: f32,
    threshold: f32,
    templates: u32,
    background: u32,
    initialization: u32,
    motion: u32,
    ready: u8,
    calibrated: u8,
    present: u8,
    reserved: u8,
    background_average: f32,
    basis_wander: f32,
    learned_baseline: f32,
    train_status: u32,
    train_action: u32,
}
#[cfg(target_os = "espidf")]
#[no_mangle]
pub static presence_rust_abi: [u32; 10] = [
    std::mem::size_of::<Diagnostic>() as u32,
    std::mem::align_of::<Diagnostic>() as u32,
    std::mem::offset_of!(Diagnostic, templates) as u32,
    std::mem::offset_of!(Diagnostic, ready) as u32,
    std::mem::offset_of!(Diagnostic, reserved) as u32,
    std::mem::offset_of!(Diagnostic, background_average) as u32,
    std::mem::offset_of!(Diagnostic, basis_wander) as u32,
    std::mem::offset_of!(Diagnostic, learned_baseline) as u32,
    std::mem::offset_of!(Diagnostic, train_status) as u32,
    std::mem::offset_of!(Diagnostic, train_action) as u32,
];
extern "C" {
    fn reference_usb_install() -> i32;
    fn reference_usb_stdio();
    fn reference_usb_read(data: *mut u8, length: usize) -> i32;
    fn reference_quiet();
    fn reference_netif_init() -> i32;
    fn reference_ap(peer: *mut u8, channel: *mut u8) -> i32;
    fn presence_create(handle: *mut *mut c_void) -> i32;
    fn presence_diagnostic(handle: *mut c_void, peer: *const u8, out: *mut Diagnostic) -> i32;
    fn esp_wifi_sensing_fsm_set_amplitude_log_enabled(handle: *mut c_void, enabled: bool) -> i32;
    fn esp_wifi_sensing_fsm_add_channel(handle: *mut c_void, peer: *const u8) -> i32;
    fn esp_wifi_sensing_fsm_control(handle: *mut c_void, command: u32, params: *mut c_void) -> i32;
    fn esp_wifi_sensing_fsm_ping_router_start(handle: *mut c_void) -> i32;
    fn esp_wifi_sensing_fsm_train_start(handle: *mut c_void, peer: *const u8) -> i32;
    fn esp_wifi_sensing_fsm_train_stop(
        handle: *mut c_void,
        peer: *const u8,
        wander: *mut f32,
        jitter: *mut f32,
    ) -> i32;
}
type Result<T> = std::result::Result<T, &'static str>;

fn line(deadline: Instant) -> Result<String> {
    let mut buffer = [0; 64];
    for i in 0..buffer.len() {
        sdk::read_exact(&mut buffer[i..i + 1], deadline)?;
        if buffer[i] == b'\n' {
            return std::str::from_utf8(&buffer[..i])
                .map(str::to_owned)
                .map_err(|_| "command");
        }
    }
    Err("command_length")
}

fn run() -> Result<()> {
    println!("PRESENCE_COMMAND_READY force=0");
    std::io::stdout().flush().map_err(|_| "output")?;
    let command = line(Instant::now() + Duration::from_secs(90))?;
    let args: Vec<_> = command.split_whitespace().collect();
    if args.len() != 3
        || !matches!(
            args[0],
            "PRESENCE" | "PRESENCE_CAPTURE" | "PRESENCE_TRACE" | "PRESENCE_CALIBRATED"
        )
    {
        return Err("command");
    }
    let corrected = args[0] == "PRESENCE_CALIBRATED";
    let rf_trace = matches!(args[0], "PRESENCE_TRACE" | "PRESENCE_CALIBRATED");
    let snapshots = args[0] != "PRESENCE";
    let amplitude = match args[2] {
        "log" => true,
        "linear" => false,
        _ => return Err("command"),
    };
    if corrected && amplitude {
        return Err("calibrated_requires_linear");
    }
    let seconds = args[1]
        .parse::<u64>()
        .ok()
        .filter(|s| (30..=600).contains(s))
        .ok_or("command")?;
    println!(
        "PRESENCE_OPTIONS seconds={seconds} force=0 engine=espressif_full amplitude={}{}{}",
        args[2],
        if rf_trace {
            " snapshots=1 frozen_inputs=1 rf_trace=1 enqueue_frozen=1 gain_warmup=1 fault_site=1 peer_scope=associated_ap"
        } else if snapshots {
            " snapshots=1 frozen_inputs=1"
        } else {
            ""
        },
        if corrected {
            " calibration=post_training_v1"
        } else {
            ""
        }
    );
    println!("PRESENCE_CREDENTIALS_READY");
    std::io::stdout().flush().map_err(|_| "output")?;
    let deadline = Instant::now() + Duration::from_secs(60);
    let mut lengths = [0; 2];
    sdk::read_exact(&mut lengths, deadline)?;
    let (slen, plen) = (lengths[0] as usize, lengths[1] as usize);
    if slen == 0 || slen > 32 || plen > 63 || (plen != 0 && plen < 8) {
        return Err("credentials_length");
    }
    let mut credentials = [0; 95];
    sdk::read_exact(&mut credentials[..slen + plen], deadline)?;
    let station = sdk::Station::connect(&credentials[..slen], &credentials[slen..slen + plen]);
    for byte in &mut credentials {
        unsafe {
            std::ptr::write_volatile(byte, 0);
        }
    }
    let station = station?;
    if snapshots && !capture::RECORDER.prepare() {
        return Err("snapshot_prepare");
    }
    capture::RECORDER
        .enabled
        .store(snapshots, std::sync::atomic::Ordering::Release);
    // The external reference has unqualified task/callback retirement. Retain
    // its station and engine until the host performs a whole-chip reset; never
    // free callback-related resources using an assumed quiescence barrier.
    std::mem::forget(station);
    let mut peer = [0; 6];
    let mut channel = 0;
    unsafe {
        sdk::checked(reference_ap(peer.as_mut_ptr(), &mut channel))?;
    }
    if rf_trace && !trace::TRACE.select_source(peer) {
        return Err("trace_source");
    }
    if rf_trace && !trace::TRACE.prepare() {
        return Err("trace_prepare");
    }
    if corrected && !calibration::RUNTIME.prepare(calibration::Config::EMPTY_HOLDOUT) {
        return Err("calibration_prepare");
    }
    trace::TRACE
        .enabled
        .store(rf_trace, std::sync::atomic::Ordering::Release);
    let mut engine = std::ptr::null_mut();
    unsafe {
        sdk::checked(presence_create(&mut engine))?;
        if engine.is_null() {
            return Err("engine_handle");
        }
        // Select the public upstream runtime option before admitting a peer.
        // No templates, thresholds or numerical results are changed by this fixture.
        if !amplitude {
            sdk::checked(esp_wifi_sensing_fsm_set_amplitude_log_enabled(
                engine, false,
            ))?;
        }
        sdk::checked(esp_wifi_sensing_fsm_add_channel(engine, peer.as_ptr()))?;
        sdk::checked(esp_wifi_sensing_fsm_control(
            engine,
            0,
            std::ptr::null_mut(),
        ))?;
        sdk::checked(esp_wifi_sensing_fsm_ping_router_start(engine))?;
    }
    println!("PRESENCE_BEGIN seconds={seconds} channel={channel} calibration=required rf_freshness=not_exported");
    std::io::stdout().flush().map_err(|_| "output")?;
    let started = Instant::now();
    let mut report_at = Instant::now();
    let mut input = Vec::with_capacity(32);
    let mut training = false;
    let mut reports = 0;
    while started.elapsed() < Duration::from_secs(seconds) {
        let mut byte = [0];
        let n = unsafe { reference_usb_read(byte.as_mut_ptr(), 1) };
        if n < 0 {
            return Err("input");
        }
        if n > 0 {
            if byte[0] == b'\n' {
                let snapshot_tag = input
                    .strip_prefix(b"SNAPSHOT ")
                    .and_then(|value| std::str::from_utf8(value).ok()?.parse::<u32>().ok());
                match input.as_slice() {
                    _ if snapshots && snapshot_tag.is_some() => {
                        let tag = snapshot_tag.unwrap();
                        if !capture::RECORDER.arm(tag) || (rf_trace && !trace::TRACE.arm(tag)) {
                            return Err("snapshot_admission");
                        }
                        println!("PRESENCE_SNAPSHOT_ARMED tag={tag}");
                    }
                    b"TRAIN_START" if !training => {
                        if corrected
                            && calibration::RUNTIME
                                .update(|value| value.snapshot(calibration::now_us()).phase)
                                != Some(calibration::Phase::Uncalibrated)
                        {
                            return Err("calibration_retrain");
                        }
                        unsafe {
                            sdk::checked(esp_wifi_sensing_fsm_train_start(engine, peer.as_ptr()))?;
                        }
                        training = true;
                        println!("PRESENCE_TRAIN_START ms={}", started.elapsed().as_millis());
                    }
                    b"TRAIN_STOP" if training => {
                        let (mut wander, mut jitter) = (0.0, 0.0);
                        let status = unsafe {
                            esp_wifi_sensing_fsm_train_stop(
                                engine,
                                peer.as_ptr(),
                                &mut wander,
                                &mut jitter,
                            )
                        };
                        training = false;
                        println!("PRESENCE_TRAIN_STOP ms={} status={status} wander={wander:.8} jitter={jitter:.8}", started.elapsed().as_millis());
                    }
                    b"CALIBRATE_BACKGROUND" if corrected && !training => {
                        let mut d = Diagnostic::default();
                        unsafe {
                            sdk::checked(presence_diagnostic(engine, peer.as_ptr(), &mut d))?;
                        }
                        if d.calibrated == 0
                            || d.train_status != 2
                            || !trace::TRACE
                                .gain_ready
                                .load(std::sync::atomic::Ordering::Acquire)
                            || trace::TRACE
                                .faults
                                .load(std::sync::atomic::Ordering::Acquire)
                                != 0
                            || calibration::RUNTIME
                                .update(|value| value.start(calibration::now_us(), d.templates))
                                != Some(true)
                        {
                            return Err("calibration_background_admission");
                        }
                        println!("PRESENCE_CALIBRATION_CONTROL phase=background");
                    }
                    b"CALIBRATE_VALIDATE" if corrected && !training => {
                        if calibration::RUNTIME
                            .update(|value| value.validate(calibration::now_us()))
                            != Some(true)
                        {
                            return Err("calibration_holdout_admission");
                        }
                        println!("PRESENCE_CALIBRATION_CONTROL phase=holdout");
                    }
                    b"CALIBRATE_ACCEPT" if corrected && !training => {
                        if calibration::RUNTIME.update(|value| value.accept(calibration::now_us()))
                            != Some(true)
                        {
                            return Err("calibration_accept_admission");
                        }
                        println!("PRESENCE_CALIBRATION_CONTROL phase=ready");
                    }
                    b"END" => break,
                    _ => return Err("control_command"),
                }
                input.clear();
            } else if input.len() < 32 {
                input.push(byte[0]);
            } else {
                return Err("control_length");
            }
        }
        if Instant::now() >= report_at {
            let mut d = Diagnostic::default();
            unsafe {
                sdk::checked(presence_diagnostic(engine, peer.as_ptr(), &mut d))?;
            }
            if ![d.wander, d.jitter, d.average, d.threshold]
                .iter()
                .all(|v| v.is_finite() && *v >= 0.0)
            {
                return Err("nonfinite_diagnostic");
            }
            let state = if training || d.calibrated == 0 || d.ready == 0 {
                "unknown"
            } else if d.present != 0 {
                "presence_hint"
            } else {
                "not_detected"
            };
            println!("PRESENCE_SAMPLE ms={} state={state} motion={} init={} training={} ready={} calibrated={} wander={:.8} jitter={:.8} average={:.8} threshold={:.8} templates={} background={} advisory=1 rf_freshness=not_exported",
                started.elapsed().as_millis(),d.motion,d.initialization,u8::from(training),d.ready,d.calibrated,
                d.wander,d.jitter,d.average,d.threshold,d.templates,d.background);
            numerical::report();
            if corrected {
                let v = calibration::RUNTIME
                    .snapshot(calibration::now_us())
                    .ok_or("calibration_status")?;
                println!("PRESENCE_CORRECTED v=1 ms={} phase={} state={} features={} average={:.8} background_min={:.8} background_max={:.8} enter={:.8} exit={:.8} advisory=1",
                    started.elapsed().as_millis(),v.phase.name(),v.state.name(),v.features,v.average,v.lower,v.upper,v.enter,v.exit);
            }
            if snapshots {
                println!("PRESENCE_CALIBRATION ms={} status={} action={} background_average={:.8} basis={:.8} baseline={:.8}",
                    started.elapsed().as_millis(),d.train_status,d.train_action,d.background_average,d.basis_wander,d.learned_baseline);
                capture::RECORDER
                    .dump(&mut std::io::stdout().lock())
                    .map_err(|_| "snapshot_output")?;
                let read =
                    |n: &std::sync::atomic::AtomicU32| n.load(std::sync::atomic::Ordering::Relaxed);
                println!(
                    "PRESENCE_CAPTURE_STATUS skipped={} foreign={} invalid={} static_bytes={}",
                    read(&capture::RECORDER.skipped),
                    read(&capture::RECORDER.foreign),
                    read(&capture::RECORDER.invalid),
                    std::mem::size_of::<capture::Recorder>()
                );
            }
            if rf_trace {
                trace::TRACE
                    .dump(&mut std::io::stdout().lock())
                    .map_err(|_| "trace_output")?;
                println!(
                    "PRESENCE_TRACE_STATUS faults={} first_fault_line={} rf_evictions={} gain_unavailable_rows={} gain_ready={} other_peer_frames={} static_bytes={}",
                    trace::TRACE
                        .faults
                        .load(std::sync::atomic::Ordering::Relaxed),
                    trace::TRACE.first_fault_line.load(std::sync::atomic::Ordering::Acquire),
                    trace::TRACE
                        .rf_evictions
                        .load(std::sync::atomic::Ordering::Relaxed),
                    trace::TRACE.gain_unavailable_rows.load(std::sync::atomic::Ordering::Relaxed),
                    u8::from(trace::TRACE.gain_ready.load(std::sync::atomic::Ordering::Acquire)),
                    trace::TRACE.other_peer_frames.load(std::sync::atomic::Ordering::Relaxed),
                    std::mem::size_of::<trace::Trace>()
                );
            }
            reports += 1;
            std::io::stdout().flush().map_err(|_| "output")?;
            report_at = Instant::now() + Duration::from_millis(500);
        }
    }
    println!(
        "PRESENCE_END ms={} reports={reports} state=unknown resources=retained_until_reset",
        started.elapsed().as_millis()
    );
    std::io::stdout().flush().map_err(|_| "output")?;
    Ok(())
}

#[no_mangle]
pub extern "C" fn presence_main() {
    std::panic::set_hook(Box::new(|_| eprintln!("PRESENCE_FAIL reason=panic")));
    let result = unsafe {
        sdk::checked(reference_usb_install()).and_then(|_| {
            reference_usb_stdio();
            reference_quiet();
            sdk::checked(reference_netif_init())
        })
    }
    .and_then(|_| run());
    if let Err(reason) = result {
        eprintln!("PRESENCE_FAIL reason={reason}");
    }
    loop {
        std::thread::sleep(Duration::from_secs(1));
    }
}
