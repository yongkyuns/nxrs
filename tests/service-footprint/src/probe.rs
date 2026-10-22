//! Optional phase clocks. Disabled counters and stamps occupy no storage.
#[cfg(feature = "probe")]
mod selected {
    #[derive(Clone, Copy, Default)]
    pub struct Counter(u64);
    pub type Stamp = u32;
    pub const ENABLED: bool = true;
    pub fn start() -> Stamp {
        crate::cycles()
    }
    impl Counter {
        pub fn record(&mut self, start: Stamp) {
            self.0 += u64::from(crate::cycles().wrapping_sub(start));
        }
    }
    impl std::ops::AddAssign for Counter {
        fn add_assign(&mut self, other: Self) {
            self.0 += other.0;
        }
    }
    impl std::ops::Div<u64> for Counter {
        type Output = u64;
        fn div(self, denominator: u64) -> u64 {
            self.0 / denominator
        }
    }
}
#[cfg(not(feature = "probe"))]
mod selected {
    #[derive(Clone, Copy, Default)]
    pub struct Counter;
    pub struct Stamp;
    pub const ENABLED: bool = false;
    pub fn start() -> Stamp {
        Stamp
    }
    impl Counter {
        pub fn record(&mut self, _: Stamp) {}
    }
    impl std::ops::AddAssign for Counter {
        fn add_assign(&mut self, _: Self) {}
    }
    impl std::ops::Div<u64> for Counter {
        type Output = u64;
        fn div(self, _: u64) -> u64 {
            0
        }
    }
}
pub use selected::*;
