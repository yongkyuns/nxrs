//! Execute the actual native Rust owner with a real File and injected controls.
//! This checks ownership/error handling, not the target ioctl ABI or hardware.
#[path = "platform/nuttx/mod.rs"]
mod native;

use crate::Error;
use embedded_hal::pwm::SetDutyCycle;
use std::{
    fs::OpenOptions,
    num::NonZeroU32,
    path::PathBuf,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Mutex,
    },
};

#[derive(Debug, PartialEq, Eq)]
enum Call {
    Configure(u32, u16),
    Start,
    Stop,
}
#[derive(Default)]
struct State {
    calls: Vec<Call>,
    fail: Option<&'static str>,
}
static STATE: Mutex<State> = Mutex::new(State {
    calls: Vec::new(),
    fail: None,
});
static SERIAL: Mutex<()> = Mutex::new(());
static NEXT: AtomicUsize = AtomicUsize::new(0);

fn record(call: Call, name: &'static str) -> i32 {
    let mut state = STATE.lock().unwrap();
    state.calls.push(call);
    if state.fail == Some(name) {
        state.fail = None;
        -1
    } else {
        0
    }
}
#[unsafe(no_mangle)]
extern "C" fn pwm_configure(_: i32, frequency: u32, duty: u16) -> i32 {
    record(Call::Configure(frequency, duty), "configure")
}
#[unsafe(no_mangle)]
extern "C" fn pwm_start_output(_: i32) -> i32 {
    record(Call::Start, "start")
}
#[unsafe(no_mangle)]
extern "C" fn pwm_stop_output(_: i32) -> i32 {
    record(Call::Stop, "stop")
}

struct FileFixture(PathBuf);
impl FileFixture {
    fn new() -> Self {
        *STATE.lock().unwrap() = State::default();
        let path = std::env::temp_dir().join(format!(
            "nxrs-pwm-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        Self(path)
    }
    fn path(&self) -> &str {
        self.0.to_str().unwrap()
    }
}
impl Drop for FileFixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}
fn frequency() -> NonZeroU32 {
    NonZeroU32::new(25).unwrap()
}
fn take_calls() -> Vec<Call> {
    std::mem::take(&mut STATE.lock().unwrap().calls)
}
fn fail(operation: &'static str) {
    STATE.lock().unwrap().fail = Some(operation);
}

#[test]
fn acquiring_output_stops_without_starting_a_waveform() {
    let _serial = SERIAL.lock().unwrap();
    let file = FileFixture::new();
    let mut output = native::Output::open(file.path()).unwrap();
    assert_eq!(take_calls(), [Call::Stop]);
    assert_eq!(output.set_duty_cycle(10), Err(Error::Unsupported));
    assert!(take_calls().is_empty());
    output.configure(frequency(), 16384).unwrap();
    output.set_duty_cycle(32768).unwrap();
    output.disable().unwrap();
    output.disable().unwrap();
    output
        .configure(NonZeroU32::new(10).unwrap(), 8192)
        .unwrap();
    output.stop().unwrap();
    assert_eq!(
        take_calls(),
        [
            Call::Configure(25, 16384),
            Call::Start,
            Call::Configure(25, 32768),
            Call::Stop,
            Call::Configure(10, 8192),
            Call::Start,
            Call::Stop,
        ]
    );
}

#[test]
fn failed_disable_retains_uncertainty_for_explicit_retry() {
    let _serial = SERIAL.lock().unwrap();
    let file = FileFixture::new();
    let mut output = native::Output::start(file.path(), frequency(), 32768).unwrap();
    take_calls();
    fail("stop");
    assert_eq!(output.disable(), Err(Error::Io));
    output.disable().unwrap();
    drop(output);
    assert_eq!(take_calls(), [Call::Stop, Call::Stop]);
}

#[test]
fn consuming_stop_reports_error_and_drop_retries_the_control() {
    let _serial = SERIAL.lock().unwrap();
    let file = FileFixture::new();
    let output = native::Output::start(file.path(), frequency(), 32768).unwrap();
    take_calls();
    fail("stop");
    assert_eq!(output.stop(), Err(Error::Io));
    assert_eq!(take_calls(), [Call::Stop, Call::Stop]);
}

#[test]
fn failed_initial_stop_or_waveform_start_never_silently_skips_cleanup() {
    let _serial = SERIAL.lock().unwrap();
    let file = FileFixture::new();
    fail("stop");
    assert!(matches!(native::Output::open(file.path()), Err(Error::Io)));
    assert_eq!(take_calls(), [Call::Stop, Call::Stop]);
    for operation in ["configure", "start"] {
        fail(operation);
        assert!(matches!(
            native::Output::start(file.path(), frequency(), 32768),
            Err(Error::Io)
        ));
        let calls = take_calls();
        assert_eq!(calls.first(), Some(&Call::Stop));
        assert_eq!(calls.last(), Some(&Call::Stop));
        assert!(calls.contains(&Call::Configure(25, 32768)));
        assert_eq!(calls.contains(&Call::Start), operation == "start");
    }
}
