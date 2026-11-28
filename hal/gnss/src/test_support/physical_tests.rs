//! Production static ownership with fake task-context UART/protocol operations.
use super::physical::{Instance, Serial};
use crate::aiding::{Aid, Capabilities, Outcome, SubmitError, TimeAid};
use crate::{Device, DeviceError, Session};
use peripheral::gnss::aiding::Progress;
use peripheral::gnss::{Error, Measurement, Receiver, Report};
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    time::{Duration, Instant},
};
static SERIAL: Mutex<()> = Mutex::new(());
static FAIL_CONFIG: AtomicBool = AtomicBool::new(false);
static FAIL_READ: AtomicBool = AtomicBool::new(false);
static FAIL_RESTORE: AtomicBool = AtomicBool::new(false);
static HOLD_AID: AtomicBool = AtomicBool::new(false);
struct Source {
    io: Serial,
    epoch: u64,
    aiding: bool,
    pending: bool,
}
impl AsRef<Serial> for Source {
    fn as_ref(&self) -> &Serial {
        &self.io
    }
}
impl AsMut<Serial> for Source {
    fn as_mut(&mut self) -> &mut Serial {
        &mut self.io
    }
}
impl Receiver for Source {
    fn aiding_capabilities(&self) -> Capabilities {
        Capabilities {
            time: self.aiding,
            position: false,
        }
    }
    fn begin_aiding(&mut self, aid: Aid, now_us: u64, _: Instant) -> Result<(), SubmitError> {
        aid.validate(now_us)?;
        self.pending = true;
        Ok(())
    }
    fn advance_aiding(&mut self, _: Instant, _: u64) -> Result<Progress, Error> {
        if HOLD_AID.load(Ordering::Acquire) {
            Ok(Progress::AwaitingAck)
        } else {
            assert!(self.pending);
            self.pending = false;
            Ok(Progress::Complete(Ok(Outcome::Accepted)))
        }
    }
    fn cancel_aiding(&mut self) -> Result<(), Error> {
        self.pending = false;
        Ok(())
    }
    fn configure(&mut self, _: Instant) -> Result<(), Error> {
        if FAIL_CONFIG.swap(false, Ordering::AcqRel) {
            Err(Error::Io)
        } else {
            Ok(())
        }
    }
    fn restore(&mut self, _: Instant) -> Result<(), Error> {
        if FAIL_RESTORE.swap(false, Ordering::AcqRel) {
            Err(Error::Io)
        } else {
            Ok(())
        }
    }
    fn read_available(
        &mut self,
        mut emit: impl FnMut(Result<Report<'_>, Error>),
    ) -> Result<(), Error> {
        if FAIL_READ.swap(false, Ordering::AcqRel) {
            return Err(Error::Io);
        }
        self.epoch += 1;
        emit(Ok(Report::Fix(Measurement {
            epoch_ms: self.epoch,
            receiver_utc_ms: None,
            latitude_deg: 1.0,
            longitude_deg: 2.0,
            speed_mps: 0.0,
            course_rad: None,
            quality: None,
        })));
        Ok(())
    }
}
fn instance(id: &'static str) -> &'static Instance<Source> {
    Box::leak(Box::new(Instance::new(
        id,
        "/dev/null",
        38400,
        65536,
        |io| Source {
            io,
            epoch: 0,
            aiding: false,
            pending: false,
        },
    )))
}
fn aiding_instance() -> &'static Instance<Source> {
    Box::leak(Box::new(Instance::new(
        "aided",
        "/dev/null",
        38400,
        65536,
        |io| Source {
            io,
            epoch: 0,
            aiding: true,
            pending: false,
        },
    )))
}
fn time_hint() -> Aid {
    Aid::Time(TimeAid {
        unix_time_ms: 1_800_000_000_000,
        observed_at_us: crate::monotonic_us(),
        uncertainty: Duration::from_millis(20),
        max_age: Duration::from_secs(60),
        drift_ppm: 20,
    })
}
#[test]
fn canonical_alias_busy_independent_instances_and_joined_reopen() {
    let _lock = SERIAL.lock().unwrap();
    let (a, b) = (instance("a"), instance("b"));
    let mut first = a.open(|_| Ok(())).unwrap();
    assert!(matches!(a.open(|_| Ok(())), Err(DeviceError::Busy)));
    let mut second = b.open(|_| Ok(())).unwrap();
    first.join().unwrap();
    second.join().unwrap();
    a.open(|_| Ok(())).unwrap().join().unwrap();
}
#[test]
fn setup_rollback_and_fault_are_scoped_to_one_device() {
    let _lock = SERIAL.lock().unwrap();
    let (a, b) = (instance("a"), instance("b"));
    FAIL_CONFIG.store(true, Ordering::Release);
    assert!(matches!(a.open(|_| Ok(())), Err(DeviceError::Io)));
    a.open(|_| Ok(())).unwrap().join().unwrap();
    super::events::FAIL_PIPE.store(true, Ordering::Release);
    assert!(matches!(a.open(|_| Ok(())), Err(DeviceError::Io)));
    let mut session = a.open(|_| Ok(())).unwrap();
    FAIL_RESTORE.store(true, Ordering::Release);
    assert_eq!(session.join(), Err(DeviceError::Io));
    assert!(matches!(a.open(|_| Ok(())), Err(DeviceError::Io)));
    let mut session = b.open(|_| Ok(())).unwrap();
    super::ffi::FAIL_RESTORE.store(true, Ordering::Release);
    assert_eq!(session.join(), Err(DeviceError::Io));
    assert!(matches!(b.open(|_| Ok(())), Err(DeviceError::Io)));
}
#[test]
fn ownership_outlives_sink_destructor_and_dropped_session() {
    let _lock = SERIAL.lock().unwrap();
    let device = instance("retirement");
    struct Sink(&'static Instance<Source>, std::sync::mpsc::SyncSender<()>);
    impl Drop for Sink {
        fn drop(&mut self) {
            assert!(matches!(self.0.open(|_| Ok(())), Err(DeviceError::Busy)));
            self.1.send(()).unwrap();
        }
    }
    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    let sink = Sink(device, tx);
    let session = device
        .open(move |_| {
            let _ = &sink;
            Ok(())
        })
        .unwrap();
    drop(session);
    rx.recv_timeout(Duration::from_secs(2)).unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        match device.open(|_| Ok(())) {
            Ok(mut session) => {
                session.join().unwrap();
                break;
            }
            Err(DeviceError::Busy) => {
                assert!(Instant::now() < deadline);
                std::thread::yield_now();
            }
            Err(e) => panic!("unexpected {e:?}"),
        }
    }
}

#[test]
fn terminal_backpressure_retains_the_device_until_join() {
    let _lock = SERIAL.lock().unwrap();
    let device = instance("terminal");
    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    FAIL_READ.store(true, Ordering::Release);
    let mut session = device
        .open(move |event| {
            assert!(matches!(event, crate::Event::Failed(DeviceError::Io)));
            let _ = tx.try_send(());
            Err(crate::Rejected {
                value: event,
                error: crate::DeliveryError::Full,
            })
        })
        .unwrap();
    rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert!(matches!(device.open(|_| Ok(())), Err(DeviceError::Busy)));
    assert_eq!(session.join(), Err(DeviceError::Io));
    device.open(|_| Ok(())).unwrap().join().unwrap();
}

#[test]
fn aiding_result_survives_full_sink_while_fixes_continue_and_reopen_is_isolated() {
    let _lock = SERIAL.lock().unwrap();
    let device = aiding_instance();
    let fixes = std::sync::Arc::new(std::sync::atomic::AtomicU32::new(0));
    let produced = fixes.clone();
    let (ready, received) = std::sync::mpsc::sync_channel(1);
    let mut session = device
        .open(move |event| {
            match event {
                crate::Event::Fix(_) => {
                    produced.fetch_add(1, Ordering::Relaxed);
                }
                crate::Event::AidingReady => {
                    let _ = ready.try_send(());
                }
                crate::Event::Failed(error) => panic!("aiding is not a receiver fault: {error:?}"),
                _ => {}
            }
            Err(crate::Rejected {
                value: event,
                error: crate::DeliveryError::Full,
            })
        })
        .unwrap();
    let mut handle = session.aiding().unwrap();
    assert!(session.aiding().is_none());
    assert_eq!(
        handle.capabilities(),
        Capabilities {
            time: true,
            position: false
        }
    );
    let id = handle.try_submit(time_hint()).unwrap();
    received.recv_timeout(Duration::from_secs(1)).unwrap();
    let completion = handle.try_completion().unwrap();
    assert_eq!(
        (completion.id, completion.result),
        (id, Ok(Outcome::Accepted))
    );
    let before = fixes.load(Ordering::Relaxed);
    let deadline = Instant::now() + Duration::from_secs(1);
    while fixes.load(Ordering::Relaxed) == before {
        assert!(Instant::now() < deadline);
        std::thread::yield_now();
    }
    session.request_stop().unwrap();
    assert_eq!(handle.try_submit(time_hint()), Err(SubmitError::Stopped));
    session.join().unwrap();
    let mut reopened = device.open(|_| Ok(())).unwrap();
    assert!(reopened.aiding().is_some());
    assert_eq!(handle.try_submit(time_hint()), Err(SubmitError::Stopped));
    reopened.join().unwrap();
}

#[test]
fn stop_cancels_an_admitted_hint_without_terminating_the_receiver_as_failed() {
    let _lock = SERIAL.lock().unwrap();
    let device = aiding_instance();
    HOLD_AID.store(true, Ordering::Release);
    let mut session = device
        .open(|event| {
            assert!(!matches!(event, crate::Event::Failed(_)));
            Ok(())
        })
        .unwrap();
    let mut handle = session.aiding().unwrap();
    let id = handle.try_submit(time_hint()).unwrap();
    session.request_stop().unwrap();
    session.join().unwrap();
    assert_eq!(
        handle.try_completion(),
        Some(crate::aiding::Completion {
            id,
            result: Ok(Outcome::Cancelled)
        })
    );
    HOLD_AID.store(false, Ordering::Release);
    device.open(|_| Ok(())).unwrap().join().unwrap();
}
