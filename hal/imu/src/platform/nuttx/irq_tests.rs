//! Source-injected witnesses for the NuttX IMU resource lifecycle.
use core::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::{
    format,
    fs::{self, File, OpenOptions},
    io,
    os::{fd::OwnedFd, unix::net::UnixStream},
    path::PathBuf,
    sync::Mutex,
    vec::Vec,
};

static OWNED: AtomicU8 = AtomicU8::new(0);
static SERIAL: Mutex<()> = Mutex::new(());

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Op {
    Stop,
    Detach,
}

struct Fake {
    regs: [u8; 128],
    bad_init: bool,
    bad_config_readback: bool,
    fail_register: bool,
    fail_unregister: bool,
    fail_stop: bool,
    irq_asserted: bool,
    registrations: usize,
    unregisters: usize,
    transfers: usize,
    last_endpoint: Option<(u8, u32)>,
    ops: Vec<Op>,
}

impl Fake {
    const fn new() -> Self {
        let mut regs = [0; 128];
        regs[0] = 0x24;
        regs[0x03] = 0xc0;
        regs[0x21] = 1;
        regs[0x0f] = 0x6c;
        Self {
            regs,
            bad_init: false,
            bad_config_readback: false,
            fail_register: false,
            fail_unregister: false,
            fail_stop: false,
            irq_asserted: true,
            registrations: 0,
            unregisters: 0,
            transfers: 0,
            last_endpoint: None,
            ops: Vec::new(),
        }
    }
}

static FAKE: Mutex<Fake> = Mutex::new(Fake::new());

mod ffi {
    use super::*;

    pub(super) fn transfer(
        _: i32,
        address: u8,
        frequency: u32,
        w: &[u8],
        r: &mut [u8],
    ) -> io::Result<()> {
        let mut f = FAKE.lock().unwrap();
        f.transfers += 1;
        f.last_endpoint = Some((address, frequency));
        let reg = *w
            .first()
            .ok_or_else(|| io::Error::other("missing register"))? as usize;
        if r.is_empty() {
            if reg == 0x58 && w.get(1) == Some(&0) {
                f.ops.push(Op::Stop);
                if f.fail_stop {
                    return Err(io::Error::other("sensor stop"));
                }
            }
            if address == 0x6a && w == [0x12, 1] {
                f.regs[reg] = 0;
            } else if reg != 0x5e {
                let end = reg + w.len() - 1;
                f.regs[reg..end].copy_from_slice(&w[1..]);
            }
        } else {
            match reg {
                0x03 => r.fill(0xc0),
                0x21 => r.fill(if f.bad_init { 2 } else { 1 }),
                0x1d | 0x24 | 0x25 | 0x26 => r.fill(0),
                0x41 if f.bad_config_readback => r.fill(f.regs[reg] ^ 1),
                _ => r.copy_from_slice(&f.regs[reg..reg + r.len()]),
            }
        }
        Ok(())
    }

    pub(super) fn wait(_: i32, _: i32) -> io::Result<bool> {
        Ok(false)
    }
    pub(super) fn tick(_: i32, _: i32) -> io::Result<bool> {
        Ok(false)
    }
    pub(super) fn irq_check(_: i32) -> io::Result<()> {
        Ok(())
    }
    pub(super) fn irq_read(_: i32) -> io::Result<bool> {
        Ok(FAKE.lock().unwrap().irq_asserted)
    }
    pub(super) fn irq_register(_: i32) -> io::Result<()> {
        let mut f = FAKE.lock().unwrap();
        f.registrations += 1;
        if f.fail_register {
            Err(io::Error::other("IRQ register"))
        } else {
            Ok(())
        }
    }
    pub(super) fn irq_unregister(_: i32) -> io::Result<()> {
        let mut f = FAKE.lock().unwrap();
        f.registrations -= 1;
        f.unregisters += 1;
        f.ops.push(Op::Detach);
        if f.fail_unregister {
            Err(io::Error::other("IRQ unregister"))
        } else {
            Ok(())
        }
    }
}

use acquisition::readiness;
#[path = "device.rs"]
mod device;
#[path = "i2c.rs"]
mod i2c;
#[path = "irq.rs"]
mod irq;
use crate::session;
#[path = "signal.rs"]
mod signal;
#[path = "trace.rs"]
mod trace;

type Bmi270Sensor = crate::sensors::bmi270::Sensor<i2c::Bus, device::Delay>;
type Lsm6dsoSensor = crate::sensors::lsm6dso::Sensor<i2c::Bus>;

fn acquire<S: crate::sensors::Sensor<i2c::Bus, device::Delay>>(
    binding: device::Binding<'_>,
    config: crate::Config,
) -> Result<device::Device<S>, crate::DeviceError> {
    device::Device::acquire(binding, config, trace::Trace::default())
}

struct DeviceFile(PathBuf);
impl DeviceFile {
    fn new() -> Self {
        let path =
            std::env::temp_dir().join(format!("nxrs-imu-resource-test-{}", std::process::id()));
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
    fn missing_path(&self) -> PathBuf {
        self.0.with_extension("missing")
    }
}
impl Drop for DeviceFile {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

fn reset() {
    OWNED.store(0, Ordering::Relaxed);
    *FAKE.lock().unwrap() = Fake::new();
}
fn binding<'a>(path: &'a str, irq_path: Option<&'a str>) -> device::Binding<'a> {
    device::Binding {
        path,
        address: 0x68,
        frequency: 400_000,
        irq_path,
    }
}

fn default_config() -> crate::Config {
    crate::sensors::bmi270::DEFAULT_CONFIG
}

#[test]
fn irq_registration_rollback_and_quarantine_are_not_repeated() {
    let _serial = SERIAL.lock().unwrap();
    for (fail_register, fail_unregister) in
        [(false, false), (true, false), (true, true), (false, true)]
    {
        reset();
        let file = DeviceFile::new();
        FAKE.lock().unwrap().fail_register = fail_register;
        FAKE.lock().unwrap().fail_unregister = fail_unregister;
        match irq::Irq::open(file.path()) {
            Err(crate::DeviceError::Io) => assert!(fail_register),
            Ok(mut irq) => {
                assert!(!fail_register);
                assert!(irq.read().unwrap());
                assert_eq!(irq.unregister().is_err(), fail_unregister);
                assert!(irq.unregister().is_ok());
            }
            _ => panic!("unexpected registration result"),
        }
        let f = FAKE.lock().unwrap();
        assert_eq!(f.registrations, 0);
        assert_eq!(f.unregisters, 1);
        assert_eq!(
            OWNED.load(Ordering::Relaxed),
            if fail_unregister { 2 } else { 0 }
        );
    }
}

#[test]
fn configured_irq_failure_has_no_timer_fallback_and_i2c_failure_detaches_irq() {
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    let missing = file.missing_path();
    let missing = missing.to_str().unwrap();

    reset();
    assert_eq!(
        acquire::<Bmi270Sensor>(binding(file.path(), Some(missing)), default_config()).err(),
        Some(crate::DeviceError::Unsupported)
    );
    assert_eq!(FAKE.lock().unwrap().transfers, 0);
    assert_eq!(OWNED.load(Ordering::Relaxed), 0);

    reset();
    assert_eq!(
        acquire::<Bmi270Sensor>(binding(missing, Some(file.path())), default_config()).err(),
        Some(crate::DeviceError::Io)
    );
    let f = FAKE.lock().unwrap();
    assert_eq!((f.registrations, f.unregisters), (0, 1));
    assert_eq!(OWNED.load(Ordering::Relaxed), 0);
}

#[test]
fn unsupported_joint_rate_and_timing_request_does_not_acquire_resources() {
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    reset();
    let config = crate::Config {
        min_rate_millihz: 100_000,
        max_rate_millihz: 100_000,
        accel_range: crate::AccelRange::G2,
        gyro_range: crate::GyroRange::Dps250,
        max_delivery_interval_us: 5_000,
    };

    assert_eq!(
        acquire::<Bmi270Sensor>(binding(file.path(), Some(file.path())), config).err(),
        Some(crate::DeviceError::Unsupported)
    );
    let f = FAKE.lock().unwrap();
    assert_eq!(f.transfers, 0);
    assert_eq!((f.registrations, f.unregisters), (0, 0));
    assert_eq!(OWNED.load(Ordering::Relaxed), 0);
}

#[test]
fn both_declared_bmi270_endpoints_reach_the_same_bus_adapter() {
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    let alternate = file.missing_path();
    let _file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&alternate)
        .unwrap();
    let alternate = DeviceFile(alternate);
    for (path, address, frequency) in [
        (file.path(), 0x68, 400_000),
        (alternate.path(), 0x69, 100_000),
    ] {
        reset();
        let binding = device::Binding {
            path,
            address,
            frequency,
            irq_path: None,
        };
        let device = acquire::<Bmi270Sensor>(binding, default_config()).unwrap();
        assert_eq!(
            FAKE.lock().unwrap().last_endpoint,
            Some((address, frequency))
        );
        drop(device);
        assert_eq!(OWNED.load(Ordering::Relaxed), 0);
    }
}

#[test]
fn replacement_sensor_uses_the_same_lease_and_reports_its_exact_rate() {
    use crate::sensors::Sensor;
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    reset();
    let replacement = device::Binding {
        path: file.path(),
        address: 0x6a,
        frequency: 100_000,
        irq_path: None,
    };
    let default = <Lsm6dsoSensor as Sensor<i2c::Bus, device::Delay>>::DEFAULT;
    assert_eq!(
        acquire::<Lsm6dsoSensor>(replacement, default_config()).err(),
        Some(crate::DeviceError::Unsupported)
    );
    let device = acquire::<Lsm6dsoSensor>(
        device::Binding {
            path: file.path(),
            address: 0x6a,
            frequency: 100_000,
            irq_path: None,
        },
        default,
    )
    .unwrap();
    assert_eq!(device.applied_config().rate_millihz, 104_000);
    assert_eq!(device.applied_config().delivery_interval_us, 9_616);
    assert_eq!(FAKE.lock().unwrap().last_endpoint, Some((0x6a, 100_000)));
    assert_eq!(
        acquire::<Bmi270Sensor>(binding(file.path(), None), default_config()).err(),
        Some(crate::DeviceError::Busy)
    );
    drop(device);
    assert_eq!(OWNED.load(Ordering::Relaxed), 0);
}

#[test]
fn applied_config_matches_default_and_reports_a_negotiated_nondefault_rate() {
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    reset();
    let defaults = default_config();
    let expected_defaults = crate::AppliedConfig {
        rate_millihz: 100_000,
        accel_range: crate::AccelRange::G4,
        gyro_range: crate::GyroRange::Dps2000,
        delivery_interval_us: 10_000,
    };
    let device = acquire::<Bmi270Sensor>(binding(file.path(), None), defaults).unwrap();
    assert_eq!(device.applied_config(), expected_defaults);
    drop(device);

    reset();
    let requested = crate::Config {
        min_rate_millihz: 25_000,
        max_rate_millihz: 100_000,
        accel_range: crate::AccelRange::G4,
        gyro_range: crate::GyroRange::Dps500,
        max_delivery_interval_us: 20_000,
    };
    let device = acquire::<Bmi270Sensor>(binding(file.path(), None), requested).unwrap();
    let applied = device.applied_config();
    assert_eq!(applied.rate_millihz, 50_000);
    assert_eq!(applied.accel_range, crate::AccelRange::G4);
    assert_eq!(applied.gyro_range, crate::GyroRange::Dps500);
    assert_eq!(applied.delivery_interval_us, 20_000);
    assert_ne!(applied, expected_defaults);
    drop(device);
}

#[test]
fn partial_sensor_setup_quarantines() {
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    reset();
    FAKE.lock().unwrap().bad_init = true;
    assert_eq!(
        acquire::<Bmi270Sensor>(binding(file.path(), None), default_config()).err(),
        Some(crate::DeviceError::InvalidData)
    );
    assert_eq!(OWNED.load(Ordering::Relaxed), 2);
    let transfers = FAKE.lock().unwrap().transfers;
    assert_eq!(
        acquire::<Bmi270Sensor>(binding(file.path(), None), default_config()).err(),
        Some(crate::DeviceError::Io)
    );
    assert_eq!(FAKE.lock().unwrap().transfers, transfers);
}

#[test]
fn config_readback_mismatch_quarantines_before_publishing_device() {
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    reset();
    FAKE.lock().unwrap().bad_config_readback = true;
    assert_eq!(
        acquire::<Bmi270Sensor>(binding(file.path(), Some(file.path())), default_config()).err(),
        Some(crate::DeviceError::InvalidData)
    );
    assert_eq!(OWNED.load(Ordering::Relaxed), 2);
    let transfers = {
        let fake = FAKE.lock().unwrap();
        assert_eq!((fake.registrations, fake.unregisters), (0, 1));
        fake.transfers
    };
    assert_eq!(
        acquire::<Bmi270Sensor>(binding(file.path(), None), default_config()).err(),
        Some(crate::DeviceError::Io)
    );
    assert_eq!(FAKE.lock().unwrap().transfers, transfers);
}

#[test]
fn unpublished_device_and_joined_worker_both_stop_before_detach() {
    use crate::Imu;
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    reset();
    // A cancel setup or thread spawn failure drops the unpublished owner.
    drop(
        acquire::<Bmi270Sensor>(binding(file.path(), Some(file.path())), default_config()).unwrap(),
    );
    assert_eq!(FAKE.lock().unwrap().ops, [Op::Stop, Op::Detach]);
    assert_eq!(OWNED.load(Ordering::Relaxed), 0);

    reset();
    FAKE.lock().unwrap().irq_asserted = false;
    let device =
        acquire::<Bmi270Sensor>(binding(file.path(), Some(file.path())), default_config()).unwrap();
    let cancel = readiness::Cancel::new(|| {
        let (reader, writer) = UnixStream::pair()?;
        Ok((OwnedFd::from(reader).into(), OwnedFd::from(writer).into()))
    })
    .unwrap();
    let wake = cancel.reader.clone();
    let mut worker = session::Session::start(
        16384,
        cancel,
        |_| Ok(()),
        move |stop, emit| device.run(stop, &wake, emit),
    )
    .unwrap();
    worker.join().unwrap();
    assert!(worker.is_finished());
    assert_eq!(FAKE.lock().unwrap().ops, [Op::Stop, Op::Detach]);
    assert_eq!(OWNED.load(Ordering::Relaxed), 0);
}

#[test]
fn failed_stop_or_detach_quarantines_without_repeating_release() {
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    for (fail_stop, fail_unregister) in [(true, false), (false, true)] {
        reset();
        {
            let mut f = FAKE.lock().unwrap();
            f.fail_stop = fail_stop;
            f.fail_unregister = fail_unregister;
        }
        let device =
            acquire::<Bmi270Sensor>(binding(file.path(), Some(file.path())), default_config())
                .unwrap();
        assert_eq!(
            device.run(
                &AtomicBool::new(true),
                &File::open(file.path()).unwrap(),
                &mut |_| true
            ),
            Err(crate::DeviceError::Io)
        );
        let f = FAKE.lock().unwrap();
        assert_eq!(OWNED.load(Ordering::Relaxed), 2);
        assert_eq!((f.registrations, f.unregisters), (0, 1));
        assert_eq!(f.ops, [Op::Stop, Op::Detach]);
    }
}

#[cfg(feature = "probe")]
#[test]
fn timing_marks_worker_io_between_setup_and_cleanup() {
    use crate::probe::Timing;
    use std::sync::Arc;

    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    reset();
    let events = Arc::new(Mutex::new(Vec::new()));
    let captured = events.clone();
    let trace = trace::Trace::new(move |event| captured.lock().unwrap().push(event));
    let device = device::Device::<Bmi270Sensor>::acquire(
        binding(file.path(), Some(file.path())),
        default_config(),
        trace,
    )
    .unwrap();
    assert_eq!(
        device.run(
            &AtomicBool::new(false),
            &File::open(file.path()).unwrap(),
            &mut |_| true
        ),
        Err(crate::DeviceError::Io), // The fake IRQ stays asserted after an empty drain.
    );
    let events = events.lock().unwrap();
    let wake = events
        .iter()
        .position(|event| {
            matches!(
                event,
                Timing::Wake {
                    periodic_deadline: None,
                    ..
                }
            )
        })
        .unwrap();
    let drain = events
        .iter()
        .position(|event| matches!(event, Timing::Drain { complete: true, .. }))
        .unwrap();
    assert!(wake > 0 && drain > wake + 1 && drain + 1 < events.len());
    assert!(events
        .iter()
        .enumerate()
        .all(|(i, event)| i == wake || i == drain || matches!(event, Timing::Ioctl { .. })));
}
