//! Compile the actual IRQ ownership code against fault-injected native operations.
use core::sync::atomic::{AtomicBool, AtomicU8, AtomicUsize, Ordering};
use std::{fs::File, io, sync::Mutex};
static OWNED: AtomicU8 = AtomicU8::new(1);
static SERIAL: Mutex<()> = Mutex::new(());
static REGISTRATIONS: AtomicUsize = AtomicUsize::new(0);
static UNREGISTERS: AtomicUsize = AtomicUsize::new(0);
static FAIL_REGISTER: AtomicBool = AtomicBool::new(false);
static FAIL_UNREGISTER: AtomicBool = AtomicBool::new(false);
mod ffi {
    use super::*;
    pub(super) fn irq_check(_: i32) -> io::Result<()> {
        Ok(())
    }
    pub(super) fn irq_read(_: i32) -> io::Result<bool> {
        Ok(true)
    }
    pub(super) fn irq_register(_: i32) -> io::Result<()> {
        REGISTRATIONS.fetch_add(1, Ordering::Relaxed);
        if FAIL_REGISTER.load(Ordering::Relaxed) {
            Err(io::Error::other("attach/enable failed"))
        } else {
            Ok(())
        }
    }
    pub(super) fn irq_unregister(_: i32) -> io::Result<()> {
        REGISTRATIONS.fetch_sub(1, Ordering::Relaxed);
        UNREGISTERS.fetch_add(1, Ordering::Relaxed);
        if FAIL_UNREGISTER.load(Ordering::Relaxed) {
            Err(io::Error::other("disable/detach failed"))
        } else {
            Ok(())
        }
    }
}
#[path = "irq.rs"]
mod irq;

#[test]
fn registration_rollback_and_cleanup_quarantine_are_not_repeated() {
    let _serial = SERIAL.lock().unwrap();
    for (fail_register, fail_unregister) in
        [(false, false), (true, false), (true, true), (false, true)]
    {
        OWNED.store(1, Ordering::Relaxed);
        REGISTRATIONS.store(0, Ordering::Relaxed);
        UNREGISTERS.store(0, Ordering::Relaxed);
        FAIL_REGISTER.store(fail_register, Ordering::Relaxed);
        FAIL_UNREGISTER.store(fail_unregister, Ordering::Relaxed);
        // Ordinary host file supplies descriptor lifetime; no device access.
        let file = File::open(std::env::current_exe().unwrap()).unwrap();
        match irq::Irq::register(file) {
            Err(crate::DeviceError::Io) => assert!(fail_register),
            Ok(mut irq) => {
                assert!(!fail_register);
                assert!(irq.read().unwrap());
                assert_eq!(irq.unregister().is_err(), fail_unregister);
                assert!(irq.unregister().is_ok());
            }
            _ => panic!("unexpected registration result"),
        }
        assert_eq!(REGISTRATIONS.load(Ordering::Relaxed), 0);
        assert_eq!(UNREGISTERS.load(Ordering::Relaxed), 1);
        assert_eq!(
            OWNED.load(Ordering::Relaxed),
            if fail_unregister { 2 } else { 1 }
        );
    }
}
