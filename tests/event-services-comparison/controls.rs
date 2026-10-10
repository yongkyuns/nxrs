//! Frozen experiment modes; the C definitions are checked by host tests.
pub const WORK_SHORT: u32 = 10_000;
pub const WORK_LONG: u32 = 400_000;
pub const WORK_MEDIUM: u32 = 100_000;
pub const IO_WAIT_US: u32 = 3_000;
pub const PROFILES: [&str; 9] = [
    "normal",
    "burst",
    "overload",
    "work-short",
    "work-long",
    "hal",
    "saturation",
    "work-medium",
    "io-wait",
];
pub const fn work_iterations(profile: u32) -> u32 {
    match profile {
        3 => WORK_SHORT,
        4 => WORK_LONG,
        7 => WORK_MEDIUM,
        _ => 0,
    }
}
#[cfg(target_os = "none")]
pub const fn timer_ms() -> u32 {
    if cfg!(feature = "timer-1ms") { 1 } else { 10 }
}
