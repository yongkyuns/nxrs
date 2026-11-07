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
    fail_register: bool,
    fail_unregister: bool,
    fail_stop: bool,
    irq_asserted: bool,
    registrations: usize,
    unregisters: usize,
    transfers: usize,
    ops: Vec<Op>,
}

impl Fake {
    const fn new() -> Self {
        let mut regs = [0; 128];
        regs[0] = 0x24;
        regs[0x03] = 0xc0;
        regs[0x21] = 1;
        Self {
            regs,
            bad_init: false,
            fail_register: false,
            fail_unregister: false,
            fail_stop: false,
            irq_asserted: true,
            registrations: 0,
            unregisters: 0,
            transfers: 0,
            ops: Vec::new(),
        }
    }
}

static FAKE: Mutex<Fake> = Mutex::new(Fake::new());

mod ffi {
    use super::*;

    pub(super) fn transfer(_: i32, _: u8, _: u32, w: &[u8], r: &mut [u8]) -> io::Result<()> {
        let mut f = FAKE.lock().unwrap();
        f.transfers += 1;
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
            if reg != 0x5e {
                let end = reg + w.len() - 1;
                f.regs[reg..end].copy_from_slice(&w[1..]);
            }
        } else {
            match reg {
                0x03 => r.fill(0xc0),
                0x21 => r.fill(if f.bad_init { 2 } else { 1 }),
                0x1d | 0x24 | 0x25 | 0x26 => r.fill(0),
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
#[path = "../../session.rs"]
mod session;
#[path = "signal.rs"]
mod signal;

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
        device::Device::acquire(binding(file.path(), Some(missing))).err(),
        Some(crate::DeviceError::Unsupported)
    );
    assert_eq!(FAKE.lock().unwrap().transfers, 0);
    assert_eq!(OWNED.load(Ordering::Relaxed), 0);

    reset();
    assert_eq!(
        device::Device::acquire(binding(missing, Some(file.path()))).err(),
        Some(crate::DeviceError::Io)
    );
    let f = FAKE.lock().unwrap();
    assert_eq!((f.registrations, f.unregisters), (0, 1));
    assert_eq!(OWNED.load(Ordering::Relaxed), 0);
}

#[test]
fn partial_sensor_setup_quarantines() {
    let _serial = SERIAL.lock().unwrap();
    let file = DeviceFile::new();
    reset();
    FAKE.lock().unwrap().bad_init = true;
    assert_eq!(
        device::Device::acquire(binding(file.path(), None)).err(),
        Some(crate::DeviceError::InvalidData)
    );
    assert_eq!(OWNED.load(Ordering::Relaxed), 2);
    let transfers = FAKE.lock().unwrap().transfers;
    assert_eq!(
        device::Device::acquire(binding(file.path(), None)).err(),
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
    drop(device::Device::acquire(binding(file.path(), Some(file.path()))).unwrap());
    assert_eq!(FAKE.lock().unwrap().ops, [Op::Stop, Op::Detach]);
    assert_eq!(OWNED.load(Ordering::Relaxed), 0);

    reset();
    FAKE.lock().unwrap().irq_asserted = false;
    let device = device::Device::acquire(binding(file.path(), Some(file.path()))).unwrap();
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
        let device = device::Device::acquire(binding(file.path(), Some(file.path()))).unwrap();
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
