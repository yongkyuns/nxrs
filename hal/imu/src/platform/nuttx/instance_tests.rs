//! Device state and storage tests, with normalized test sensors.
use super::{events, test_support};
use crate::{AppliedConfig, Config, Device, DeviceError, Event, Session};
use peripheral::imu::{Buffering, Error, Imu, Measurement};
use std::{
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        mpsc,
    },
    time::Duration,
};

struct Control {
    fail: AtomicBool,
    fail_stop: AtomicBool,
    calls: AtomicUsize,
    drops: AtomicUsize,
}
impl Control {
    const fn new() -> Self {
        Self {
            fail: AtomicBool::new(false),
            fail_stop: AtomicBool::new(false),
            calls: AtomicUsize::new(0),
            drops: AtomicUsize::new(0),
        }
    }
}
struct Sensor<const MODEL: u8>(&'static Control);
impl<const MODEL: u8> Imu for Sensor<MODEL> {
    const DEFAULT: Config = Config {
        min_rate_millihz: 100_000,
        max_rate_millihz: 100_000,
        accel_range_g: 4,
        gyro_range_dps: 2000,
        max_delivery_interval_us: 10_000,
    };
    const BUFFERING: Buffering = Buffering::Fifo;
    type Settings = ();
    fn resolve(config: Config) -> Result<((), AppliedConfig), Error> {
        peripheral::imu::select_rate(config, [50_000, 100_000], core::convert::identity)
            .map(|(_, applied)| ((), applied))
    }
    fn read_available(
        &mut self,
        mut emit: impl FnMut(Measurement) -> bool,
    ) -> Result<Option<usize>, Error> {
        if self.0.fail.load(Ordering::Acquire) {
            return Err(Error::Io);
        }
        let count = self.0.calls.fetch_add(1, Ordering::Relaxed);
        Ok(emit(Measurement {
            accel_mps2: [MODEL as f32, count as f32, 9.81],
            gyro_rps: [0.; 3],
        })
        .then_some(1))
    }
    fn stop(&mut self) -> Result<(), Error> {
        if self.0.fail_stop.load(Ordering::Acquire) {
            Err(Error::Io)
        } else {
            Ok(())
        }
    }
}
impl<const MODEL: u8> Drop for Sensor<MODEL> {
    fn drop(&mut self) {
        self.0.drops.fetch_add(1, Ordering::Release);
    }
}
fn construct<const MODEL: u8>(control: &'static Control, _: ()) -> Result<Sensor<MODEL>, Error> {
    Ok(Sensor(control))
}
type Instance<const MODEL: u8> = super::Instance<Sensor<MODEL>, &'static Control>;
static A_CONTROL: Control = Control::new();
static B_CONTROL: Control = Control::new();
static C_CONTROL: Control = Control::new();
static A: Instance<0> = Instance::new("front", None, 65_536, |_| Ok(&A_CONTROL), construct);
static B: Instance<0> = Instance::new("backup", None, 65_536, |_| Ok(&B_CONTROL), construct);
static C: Instance<1> = Instance::new("auxiliary", None, 65_536, |_| Ok(&C_CONTROL), construct);

fn primary() -> &'static impl Device {
    &A
}
fn navigation() -> &'static impl Device {
    &A
}

fn concurrent(left: &'static impl Device, right: &'static impl Device) {
    let (send, recv) = mpsc::channel();
    let other = send.clone();
    let mut first = left
        .open_default(move |event| {
            send.send((0, event)).unwrap();
            Ok(())
        })
        .unwrap();
    let mut config = right.default_config().unwrap();
    config.min_rate_millihz = 50_000;
    config.max_rate_millihz = 50_000;
    config.max_delivery_interval_us = 20_000;
    let (mut second, applied) = right
        .open(config, move |event| {
            other.send((1, event)).unwrap();
            Ok(())
        })
        .unwrap();
    assert_eq!(applied.rate_millihz, 50_000);
    let mut seen = [false; 2];
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    while !seen.iter().all(|value| *value) {
        assert!(
            std::time::Instant::now() < deadline,
            "both devices must emit"
        );
        let (id, event) = recv.recv_timeout(Duration::from_secs(2)).unwrap();
        let Event::Sample(sample) = event else {
            panic!("unexpected fault")
        };
        assert_eq!(
            sample.sample_interval_us,
            if id == 0 { 10_000 } else { 20_000 }
        );
        seen[id] = true;
    }
    assert_eq!(
        navigation().open_default(|_| Ok(())).err(),
        Some(DeviceError::Busy)
    );
    first.join().unwrap();
    second.join().unwrap();
}

#[test]
fn aliases_same_model_and_mixed_devices_have_independent_storage_and_faults() {
    let _serial = test_support::periodic_fixture();
    assert_eq!(
        primary() as *const _ as *const (),
        navigation() as *const _ as *const ()
    );
    concurrent(primary(), &B);
    concurrent(primary(), &C);
    assert_eq!(
        (
            A_CONTROL.drops.load(Ordering::Acquire),
            B_CONTROL.drops.load(Ordering::Acquire),
            C_CONTROL.drops.load(Ordering::Acquire)
        ),
        (2, 1, 1)
    );
    C_CONTROL.fail_stop.store(true, Ordering::Release);
    let mut poisoned = C.open_default(|_| Ok(())).unwrap();
    assert_eq!(poisoned.join(), Err(DeviceError::Io));
    assert_eq!(C.open_default(|_| Ok(())).err(), Some(DeviceError::Io));
    A.open_default(|_| Ok(())).unwrap().join().unwrap();
    B.open_default(|_| Ok(())).unwrap().join().unwrap();
}

static RETIRING_CONTROL: Control = Control::new();
static RETIRING: Instance<0> = Instance::new(
    "retiring",
    None,
    65_536,
    |_| Ok(&RETIRING_CONTROL),
    construct,
);
#[test]
fn terminal_backpressure_and_dropped_session_keep_the_instance_owned() {
    let _serial = test_support::periodic_fixture();
    use std::sync::Arc;
    events::FAIL_PIPE.with(|fail| fail.set(true));
    assert_eq!(
        RETIRING.open_default(|_| Ok(())).err(),
        Some(DeviceError::Io)
    );
    assert_eq!(RETIRING_CONTROL.drops.load(Ordering::Acquire), 1);
    RETIRING_CONTROL.drops.store(0, Ordering::Release);
    let release = Arc::new(AtomicBool::new(false));
    let observed = Arc::new(AtomicBool::new(false));
    let allow = release.clone();
    let delivered = observed.clone();
    RETIRING_CONTROL.fail.store(true, Ordering::Release);
    let mut session = RETIRING
        .open_default(move |event| {
            assert_eq!(event, Event::Failed(DeviceError::Io));
            delivered.store(true, Ordering::Release);
            if allow.load(Ordering::Acquire) {
                Ok(())
            } else {
                Err(messaging::Rejected {
                    value: event,
                    error: messaging::DeliveryError::Full,
                })
            }
        })
        .unwrap();
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    while !observed.load(Ordering::Acquire) && std::time::Instant::now() < deadline {
        std::thread::yield_now();
    }
    assert!(observed.load(Ordering::Acquire));
    assert_eq!(
        RETIRING.open_default(|_| Ok(())).err(),
        Some(DeviceError::Busy)
    );
    assert_eq!(RETIRING_CONTROL.drops.load(Ordering::Acquire), 0);
    release.store(true, Ordering::Release);
    assert_eq!(session.join(), Err(DeviceError::Io));
    assert_eq!(RETIRING_CONTROL.drops.load(Ordering::Acquire), 1);
    RETIRING_CONTROL.fail.store(false, Ordering::Release);
    RETIRING.open_default(|_| Ok(())).unwrap().join().unwrap();

    // Dropping the user session while its callback is in flight cannot release storage.
    let (entered, seen) = mpsc::sync_channel(1);
    let (finish, wait) = mpsc::sync_channel(1);
    let session = RETIRING
        .open_default(move |_| {
            entered.send(()).unwrap();
            wait.recv_timeout(Duration::from_secs(2)).unwrap();
            Ok(())
        })
        .unwrap();
    seen.recv_timeout(Duration::from_secs(2)).unwrap();
    drop(session);
    assert_eq!(
        RETIRING.open_default(|_| Ok(())).err(),
        Some(DeviceError::Busy)
    );
    finish.send(()).unwrap();
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    loop {
        match RETIRING.open_default(|_| Ok(())) {
            Ok(mut session) => {
                session.join().unwrap();
                break;
            }
            Err(DeviceError::Busy) if std::time::Instant::now() < deadline => {
                std::thread::yield_now()
            }
            _ => panic!("device failed to retire"),
        }
    }
}
