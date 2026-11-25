//! Host-injected resource checks; no physical GPIO is accessed.
#![cfg(unix)]
use embedded_hal::digital::InputPin;
use std::cell::{Cell, RefCell};

#[derive(Default)]
struct State {
    wrong_type: bool,
    read_failed: bool,
    detach_failed: bool,
    registered: usize,
    unregisters: usize,
    high: bool,
}
thread_local! { static STATE: RefCell<State> = RefCell::new(State::default()); }

mod ffi {
    use super::*;
    use std::io;
    pub fn irq_check(_: i32) -> io::Result<()> {
        STATE.with_borrow(|s| {
            if s.wrong_type {
                Err(io::Error::other("type"))
            } else {
                Ok(())
            }
        })
    }
    pub fn irq_register(_: i32) -> io::Result<()> {
        STATE.with_borrow_mut(|s| s.registered += 1);
        Ok(())
    }
    pub fn irq_unregister(_: i32) -> io::Result<()> {
        STATE.with_borrow_mut(|s| {
            s.unregisters += 1;
            if s.detach_failed {
                Err(io::Error::other("detach"))
            } else {
                s.registered -= 1;
                Ok(())
            }
        })
    }
    pub fn irq_read(_: i32) -> io::Result<bool> {
        STATE.with_borrow(|s| {
            if s.read_failed {
                Err(io::Error::other("read"))
            } else {
                Ok(s.high)
            }
        })
    }
}
mod support;
use support::Interrupt;

#[test]
fn embedded_hal_reads_propagate_level_and_io_failure() {
    STATE.set(State::default());
    let mut pin =
        Interrupt::open_rising("/dev/null", || panic!("unexpected cleanup fault")).unwrap();
    assert!(pin.file().metadata().is_ok());
    assert!(pin.is_low().unwrap());
    STATE.with_borrow_mut(|s| s.high = true);
    assert!(pin.is_high().unwrap());
    STATE.with_borrow_mut(|s| s.read_failed = true);
    assert_eq!(pin.is_low(), Err(gpio::Error::Io));
    drop(pin);
    STATE.with_borrow(|s| assert_eq!((s.registered, s.unregisters), (0, 1)));
}

#[test]
fn unsupported_pin_is_rejected_before_registration() {
    STATE.set(State {
        wrong_type: true,
        ..State::default()
    });
    assert_eq!(
        Interrupt::open_rising("/dev/null", || panic!("no registration")).err(),
        Some(gpio::Error::Unsupported)
    );
    STATE.with_borrow(|s| assert_eq!((s.registered, s.unregisters), (0, 0)));
}

#[test]
fn drop_failure_notifies_owner_exactly_once() {
    STATE.set(State {
        detach_failed: true,
        ..State::default()
    });
    let failures = Cell::new(0);
    drop(Interrupt::open_rising("/dev/null", || failures.set(failures.get() + 1)).unwrap());
    assert_eq!(failures.get(), 1);
    STATE.with_borrow(|s| assert_eq!((s.registered, s.unregisters), (1, 1)));
}
