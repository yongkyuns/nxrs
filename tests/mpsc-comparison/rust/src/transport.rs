use crate::{check, engine, platform};
pub trait Channel<const W: usize>: Sync {
    const NAME: &'static str;
    fn create(capacity: usize, producers: usize) -> Self;
    fn send(&self, role: usize, payload: [u32; W], blocking: bool) -> Result<(), [u32; W]>;
    /// # Safety
    /// Exactly one consumer may access the receiver through final retirement.
    unsafe fn receive(&self) -> [u32; W];
}

pub struct Native<const W: usize>;
impl<const W: usize> Channel<W> for Native<W> {
    const NAME: &'static str = "native";
    fn create(capacity: usize, _: usize) -> Self {
        platform::queue_create(capacity, W * 4);
        Self
    }
    fn send(&self, _: usize, payload: [u32; W], blocking: bool) -> Result<(), [u32; W]> {
        if platform::queue_send(&payload, blocking) {
            Ok(())
        } else {
            Err(payload)
        }
    }
    unsafe fn receive(&self) -> [u32; W] {
        let mut p = [0; W];
        platform::queue_receive(&mut p);
        p
    }
}
impl<const W: usize> Drop for Native<W> {
    fn drop(&mut self) {
        platform::queue_destroy();
    }
}

#[cfg(not(target_os = "none"))]
mod ordinary_std {
    use super::*;
    use core::cell::UnsafeCell;
    use std::sync::mpsc::{Receiver, SyncSender, TrySendError, sync_channel};
    pub struct Std<const W: usize> {
        tx: Vec<SyncSender<[u32; W]>>,
        rx: UnsafeCell<Receiver<[u32; W]>>,
    }
    // The engine admits exactly one consumer; joins it before dropping rx.
    // Each producer uses a distinct clone, retained through its joined lifetime.
    unsafe impl<const W: usize> Sync for Std<W> {}
    impl<const W: usize> Channel<W> for Std<W> {
        const NAME: &'static str = "std";
        fn create(capacity: usize, producers: usize) -> Self {
            let (tx, rx) = sync_channel(capacity);
            Self {
                tx: (0..=producers).map(|_| tx.clone()).collect(),
                rx: UnsafeCell::new(rx),
            }
        }
        fn send(&self, role: usize, p: [u32; W], blocking: bool) -> Result<(), [u32; W]> {
            let tx = &self.tx[role.saturating_sub(1)];
            if blocking {
                tx.send(p).unwrap();
                Ok(())
            } else {
                match tx.try_send(p) {
                    Ok(()) => Ok(()),
                    Err(TrySendError::Full(returned)) => Err(returned),
                    Err(_) => {
                        check(false, "disconnected");
                        Err(p)
                    }
                }
            }
        }
        unsafe fn receive(&self) -> [u32; W] {
            unsafe { &*self.rx.get() }.recv().unwrap()
        }
    }
}
#[cfg(not(target_os = "none"))]
pub fn run_std(mode: engine::Mode, n: usize, bytes: usize, capacity: usize, samples: usize) {
    if bytes == 16 {
        engine::run::<ordinary_std::Std<4>, 4>(mode, n, capacity, samples);
    } else {
        engine::run::<ordinary_std::Std<16>, 16>(mode, n, capacity, samples);
    }
}
#[cfg(target_os = "none")]
pub fn run_std(_: engine::Mode, _: usize, _: usize, _: usize, _: usize) {
    check(false, "std-not-qualified-on-this-runtime");
}

pub fn run_native(mode: engine::Mode, n: usize, bytes: usize, capacity: usize, samples: usize) {
    if bytes == 16 {
        engine::run::<Native<4>, 4>(mode, n, capacity, samples);
    } else {
        engine::run::<Native<16>, 16>(mode, n, capacity, samples);
    }
}
