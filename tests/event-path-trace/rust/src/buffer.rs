//! A finite append-only diagnostic buffer. Overflow never overwrites evidence.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Event {
    pub ticks: u32,
    pub kind: u32,
    pub tid: i32,
    pub detail: u32,
}

pub const EMPTY: Event = Event {
    ticks: 0,
    kind: 0,
    tid: 0,
    detail: 0,
};

pub struct Buffer<const N: usize> {
    pub events: [Event; N],
    pub len: usize,
    pub dropped: u32,
}

impl<const N: usize> Buffer<N> {
    pub const fn new() -> Self {
        Self {
            events: [EMPTY; N],
            len: 0,
            dropped: 0,
        }
    }

    pub fn push(&mut self, event: Event) {
        if self.len < N {
            self.events[self.len] = event;
            self.len += 1;
        } else {
            self.dropped = self.dropped.saturating_add(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn overflow_preserves_original_events_and_is_sticky() {
        let mut buffer = Buffer::<2>::new();
        for ticks in 0..4 {
            buffer.push(Event { ticks, ..EMPTY });
        }
        assert_eq!(buffer.len, 2);
        assert_eq!(buffer.dropped, 2);
        assert_eq!(buffer.events.map(|e| e.ticks), [0, 1]);
    }

    #[test]
    fn zero_capacity_is_a_bounded_failure() {
        let mut buffer = Buffer::<0>::new();
        buffer.push(EMPTY);
        assert_eq!(buffer.dropped, 1);
    }
}
