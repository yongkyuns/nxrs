//! Production static ownership with fake task-context UART/protocol operations.
use super::physical::{Instance, Serial};
use crate::{Device, DeviceError, Session};
use peripheral::gnss::{Error, Measurement, Receiver};
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
struct Source {
    io: Serial,
    epoch: u64,
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
        mut emit: impl FnMut(Result<Measurement, Error>),
    ) -> Result<(), Error> {
        if FAIL_READ.swap(false, Ordering::AcqRel) {
            return Err(Error::Io);
        }
        self.epoch += 1;
        emit(Ok(Measurement {
            epoch_ms: self.epoch,
            latitude_deg: 1.0,
            longitude_deg: 2.0,
            speed_mps: 0.0,
            course_rad: None,
        }));
        Ok(())
    }
}
fn instance(id: &'static str) -> &'static Instance<Source> {
    Box::leak(Box::new(Instance::new(
        id,
        "/dev/null",
        38400,
        65536,
        |io| Source { io, epoch: 0 },
    )))
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
