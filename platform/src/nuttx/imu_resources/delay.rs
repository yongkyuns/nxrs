use embedded_hal::delay::DelayNs;
use std::time::Duration;

pub(crate) struct Delay;

impl DelayNs for Delay {
    fn delay_ns(&mut self, ns: u32) {
        std::thread::sleep(Duration::from_nanos(u64::from(ns)));
    }
}
