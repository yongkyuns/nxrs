//! Finite activation window for the static application-credential cohort.
//! The gateway host's clock and configuration remain trusted prerequisites.
use serde::Deserialize;
use std::{cell::Cell, time::{Duration, Instant, SystemTime, UNIX_EPOCH}};

pub const MAX_WINDOW_SECONDS: u64 = 90 * 86_400;

#[derive(Clone, Copy, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Window {
    pub not_before: u64,
    pub expires_at: u64,
}
impl Window {
    pub(crate) fn validate(self) -> Result<(), &'static str> {
        if self.not_before == 0 || !self.expires_at.checked_sub(self.not_before)
            .is_some_and(|seconds| (1..=MAX_WINDOW_SECONDS).contains(&seconds)) {
            return Err("invalid credential activation window");
        }
        Ok(())
    }
}

pub(crate) struct Lease {
    window: Window,
    started: Instant,
    deadline: Instant,
    last_wall: Cell<Duration>,
    failed: Cell<bool>,
}
fn clock() -> (Option<Duration>, Instant) {
    (SystemTime::now().duration_since(UNIX_EPOCH).ok(), Instant::now())
}
impl Lease {
    pub(crate) fn new(window: Window) -> Result<Self, &'static str> {
        let (wall, now) = clock();
        Self::at(window, wall.ok_or("credential clock unavailable")?, now)
    }
    fn at(window: Window, wall: Duration, now: Instant) -> Result<Self, &'static str> {
        window.validate()?;
        if wall < Duration::from_secs(window.not_before) || wall >= Duration::from_secs(window.expires_at) {
            return Err("credential activation window unavailable");
        }
        let remaining = Duration::from_secs(window.expires_at) - wall;
        let deadline = now.checked_add(remaining).ok_or("credential deadline unavailable")?;
        Ok(Self { window, started:now, deadline, last_wall:Cell::new(wall), failed:Cell::new(false) })
    }
    pub(crate) fn valid(&self) -> bool {
        let (wall, now) = clock(); self.valid_at(wall, now)
    }
    fn valid_at(&self, wall: Option<Duration>, now: Instant) -> bool {
        let valid = !self.failed.get() && now >= self.started && now < self.deadline
            && wall.is_some_and(|at| at >= self.last_wall.get()
                && at >= Duration::from_secs(self.window.not_before)
                && at < Duration::from_secs(self.window.expires_at));
        if valid { self.last_wall.set(wall.unwrap()); }
        else { self.failed.set(true); }
        valid
    }
    pub(crate) fn deadline(&self) -> Instant { self.deadline }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn window() -> Window { Window { not_before:100, expires_at:200 } }
    #[test]
    fn rejects_missing_order_bounds_and_noncurrent_activation() {
        let now=Instant::now();
        for (begin,end) in [(0,1),(200,200),(201,200),(100,100+MAX_WINDOW_SECONDS+1)] {
            assert!(Lease::at(Window { not_before:begin,expires_at:end },Duration::from_secs(150),now).is_err());
        }
        for at in [99,200,201] { assert!(Lease::at(window(),Duration::from_secs(at),now).is_err()); }
    }
    #[test]
    fn preserves_fractional_remaining_time_and_half_open_expiry() {
        let now=Instant::now();let wall=Duration::from_millis(199_750);
        let lease=Lease::at(window(),wall,now).unwrap();
        assert_eq!(lease.deadline(),now+Duration::from_millis(250));
        assert!(lease.valid_at(Some(wall),now+Duration::from_millis(249)));
        assert!(!lease.valid_at(Some(wall),lease.deadline()));
        assert!(!lease.valid_at(Some(Duration::from_secs(150)),now));
    }
    #[test]
    fn rollback_forward_expiry_and_clock_loss_are_sticky_until_reactivation() {
        let now=Instant::now();
        for bad in [None,Some(Duration::from_secs(149)),Some(Duration::from_secs(200))] {
            let lease=Lease::at(window(),Duration::from_secs(150),now).unwrap();
            assert!(lease.valid_at(Some(Duration::from_secs(151)),now+Duration::from_secs(1)));
            assert!(!lease.valid_at(bad,now+Duration::from_secs(2)));
            assert!(!lease.valid_at(Some(Duration::from_secs(152)),now+Duration::from_secs(3)));
        }
    }
}
