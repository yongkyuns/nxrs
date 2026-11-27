use std::{
    collections::VecDeque,
    time::{Duration, Instant},
};

/// A bounded presentation history. Admission evicts the oldest sample before
/// inserting a new one, retaining at most `CAPACITY` timestamp/value pairs.
pub struct History<T, const CAPACITY: usize> {
    points: VecDeque<(Instant, T)>,
}

impl<T, const CAPACITY: usize> Default for History<T, CAPACITY> {
    fn default() -> Self {
        Self::new()
    }
}

impl<T, const CAPACITY: usize> History<T, CAPACITY> {
    /// Reserve history once. A zero-capacity history is a configuration error.
    ///
    /// # Panics
    /// Panics if `CAPACITY` is zero.
    pub fn new() -> Self {
        assert!(CAPACITY > 0, "presentation history must have capacity");
        Self {
            points: VecDeque::with_capacity(CAPACITY),
        }
    }

    pub fn record(&mut self, time: Instant, value: T) {
        if self.points.len() == CAPACITY {
            self.points.pop_front();
        }
        self.points.push_back((time, value));
    }

    /// Return samples in insertion order and their normalized horizontal
    /// position: zero at the old edge, one at `now`. An empty time window
    /// yields no points. Future timestamps are clamped to `now`.
    pub fn window(&self, now: Instant, window: Duration) -> impl Iterator<Item = (f32, &T)> + '_ {
        self.points.iter().filter_map(move |(time, value)| {
            let age = now.saturating_duration_since(*time);
            (!window.is_zero() && age <= window)
                .then(|| (1.0 - age.as_secs_f32() / window.as_secs_f32(), value))
        })
    }
}
