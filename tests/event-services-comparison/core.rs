//! Portable implementation of the event-service comparison contract.
//!
//! This module intentionally has no crate-level attributes so it can be
//! included from both `no_std` firmware and the standalone host harness.

pub const ES_SERVICES: usize = 20;
pub const ES_KINDS: usize = 3;
pub const ES_PEERS: usize = 3;
pub const ES_FLOWS: usize = 9;
pub const ES_CAPACITY: usize = 8;
pub const ES_EVENT_BYTES: usize = 64;
pub const ES_HIST_BINS: usize = 64;
pub const ES_HZ: u32 = 240;
pub const ES_DURATION_US: u32 = 2_000_000;
pub const ES_DRAIN_US: u32 = 500_000;

/// Apply a bounded, deterministic workload using wrapping 32-bit arithmetic.
/// The return value must be retained by callers so the work remains observable.
pub fn work_value(value: u32, token: u32, iterations: u32) -> u32 {
    work_value_range(value, token, 0, iterations)
}

/// Continue the identical computation at its absolute iteration index.
/// Restarting the index at each chunk would silently change the workload.
pub fn work_value_range(mut value: u32, token: u32, start: u32, iterations: u32) -> u32 {
    let mut iteration = start;
    let end = start.checked_add(iterations).expect("bounded work range");
    while iteration < end {
        let rotated = value.rotate_left(5);
        let mixed = iteration.wrapping_mul(0x7f4a_7c15);
        value = rotated
            .wrapping_mul(0x9e37_79b9)
            .wrapping_add(token ^ mixed);
        iteration += 1;
    }
    value
}

const CYCLES_PER_MS: u32 = ES_HZ * 1_000;
const DURATION_CYCLES: u32 = (ES_DURATION_US / 1_000) * CYCLES_PER_MS;
const PERIOD_MS: [u32; ES_KINDS] = [250, 50, 100];
const PEER_OFFSETS: [u32; ES_PEERS] = [1, 3, 7];
const TOKEN_SEQUENCE_MULTIPLIER: u32 = 0x9e37_79b9;
const TOKEN_XOR: u32 = 0xa5a5_a5a5;

#[repr(C)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Event {
    pub scheduled_cycles: u32,
    pub posted_cycles: u32,
    pub sequence: u32,
    pub source: u8,
    pub destination: u8,
    pub kind: u8,
    pub peer: u8,
    pub token: u32,
    pub payload: [u8; 44],
}

impl Default for Event {
    fn default() -> Self {
        Self {
            scheduled_cycles: 0,
            posted_cycles: 0,
            sequence: 0,
            source: 0,
            destination: 0,
            kind: 0,
            peer: 0,
            token: 0,
            payload: [0; 44],
        }
    }
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct Release {
    pub scheduled_cycles: u32,
    pub sequence: u32,
    pub count: u32,
    pub kind: u32,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct Schedule {
    pub next: [u32; ES_KINDS],
    pub sequence: [u32; ES_KINDS],
    pub service: u32,
    pub profile: u32,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct Flow {
    pub count: u32,
    pub digest: u32,
    pub last_sequence: u32,
    pub initialized: u32,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct State {
    pub received: [Flow; ES_FLOWS],
    pub value: u32,
    pub errors: u32,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Histogram {
    pub bins: [u32; ES_HIST_BINS],
    pub count: u32,
    pub maximum: u32,
}

impl Default for Histogram {
    fn default() -> Self {
        Self {
            bins: [0; ES_HIST_BINS],
            count: 0,
            maximum: 0,
        }
    }
}

pub const fn destination(source: u32, peer: u32) -> u32 {
    if peer < ES_PEERS as u32 {
        source.wrapping_add(PEER_OFFSETS[peer as usize]) % ES_SERVICES as u32
    } else {
        u32::MAX
    }
}

pub const fn token(source: u32, destination: u32, kind: u32, sequence: u32) -> u32 {
    ((source << 24) | (destination << 16) | (kind << 8))
        ^ sequence.wrapping_mul(TOKEN_SEQUENCE_MULTIPLIER)
        ^ TOKEN_XOR
}

const fn payload_byte(token: u32, index: usize) -> u8 {
    ((token >> ((index % 4) * 8)) as u8) ^ (index as u8)
}

impl Event {
    pub const fn new(source: u32, peer: u32, release: &Release, posted: u32) -> Self {
        let dest = destination(source, peer);
        let event_token = token(source, dest, release.kind, release.sequence);
        let mut payload = [0; 44];
        let mut i = 0;
        while i < payload.len() {
            payload[i] = payload_byte(event_token, i);
            i += 1;
        }
        Self {
            scheduled_cycles: release.scheduled_cycles,
            posted_cycles: posted,
            sequence: release.sequence,
            source: source as u8,
            destination: dest as u8,
            kind: release.kind as u8,
            peer: peer as u8,
            token: event_token,
            payload,
        }
    }
}

impl Schedule {
    pub const fn new(service: u32, profile: u32) -> Self {
        let mut next = [0; ES_KINDS];
        let mut kind = 0;
        while kind < ES_KINDS {
            let period_ms = if profile == 2 && kind == 1 {
                10
            } else {
                PERIOD_MS[kind]
            };
            let phase_ms = if profile == 2 {
                0
            } else {
                service + (2 * kind as u32)
            };
            next[kind] = (period_ms + phase_ms) * CYCLES_PER_MS;
            kind += 1;
        }
        Self {
            next,
            sequence: [0; ES_KINDS],
            service,
            profile,
        }
    }

    /// Return and advance the earliest due class release, with kind as the
    /// deterministic tie-breaker. Releases at or beyond the run duration are
    /// excluded even when `now` has passed them.
    pub fn due(&mut self, now: u32) -> Option<Release> {
        let mut selected = ES_KINDS;
        let mut kind = 0;
        while kind < ES_KINDS {
            let deadline = self.next[kind];
            if deadline < DURATION_CYCLES
                && deadline <= now
                && (selected == ES_KINDS || deadline < self.next[selected])
            {
                selected = kind;
            }
            kind += 1;
        }
        if selected == ES_KINDS {
            return None;
        }

        let batch_count = match self.profile {
            1 if selected == 1 => 2,
            2 if selected == 1 => 16,
            _ => 1,
        };
        let release = Release {
            scheduled_cycles: self.next[selected],
            sequence: self.sequence[selected],
            count: batch_count,
            kind: selected as u32,
        };
        let period = if self.profile == 2 && selected == 1 {
            10 * CYCLES_PER_MS
        } else {
            PERIOD_MS[selected] * CYCLES_PER_MS
        };
        self.next[selected] = self.next[selected].wrapping_add(period);
        self.sequence[selected] = self.sequence[selected].wrapping_add(batch_count);
        Some(release)
    }

    /// Return the next eligible deadline, or the run duration when complete.
    pub const fn next_deadline(&self) -> u32 {
        let mut earliest = DURATION_CYCLES;
        let mut kind = 0;
        while kind < ES_KINDS {
            let deadline = self.next[kind];
            if deadline < earliest && deadline < DURATION_CYCLES {
                earliest = deadline;
            }
            kind += 1;
        }
        earliest
    }
}

impl State {
    /// Validate a complete packet before updating any state.
    pub fn handle(&mut self, event: &Event, service: u32) -> Result<(), ()> {
        let source = event.source as u32;
        let dest = event.destination as u32;
        let kind = event.kind as u32;
        let peer = event.peer as u32;
        if service >= ES_SERVICES as u32
            || source >= ES_SERVICES as u32
            || dest >= ES_SERVICES as u32
            || kind >= ES_KINDS as u32
            || peer >= ES_PEERS as u32
            || dest != service
            || destination(source, peer) != service
        {
            return Err(());
        }

        let flow_index = (peer * ES_KINDS as u32 + kind) as usize;
        let flow = self.received[flow_index];
        if (flow.initialized != 0 && event.sequence <= flow.last_sequence)
            || event.token != token(source, dest, kind, event.sequence)
        {
            return Err(());
        }
        let mut i = 0;
        while i < event.payload.len() {
            if event.payload[i] != payload_byte(event.token, i) {
                return Err(());
            }
            i += 1;
        }

        let updated = &mut self.received[flow_index];
        updated.count = updated.count.wrapping_add(1);
        updated.digest = updated.digest.wrapping_add(event.token);
        updated.last_sequence = event.sequence;
        updated.initialized = 1;
        self.value = self.value.rotate_left(3) ^ event.token;
        self.value = self.value.wrapping_add(0x7f4a_7c15);
        Ok(())
    }
}

impl Histogram {
    pub fn add(&mut self, microseconds: u32) {
        let index = histogram_bin(microseconds);
        self.bins[index] = self.bins[index].wrapping_add(1);
        self.count = self.count.wrapping_add(1);
        if microseconds > self.maximum {
            self.maximum = microseconds;
        }
    }

    /// Nearest-rank percentile, reported as its conservative bin bound and
    /// capped by the largest observed sample.
    pub fn percentile(&self, percent: u32) -> u32 {
        if self.count == 0 || percent == 0 {
            return 0;
        }
        let percent = percent.min(100) as u64;
        let rank = ((self.count as u64) * percent + 99) / 100;
        let mut cumulative = 0u64;
        let mut i = 0;
        while i < ES_HIST_BINS {
            cumulative += self.bins[i] as u64;
            if cumulative >= rank {
                return histogram_bound(i).min(self.maximum);
            }
            i += 1;
        }
        self.maximum
    }
}

const fn histogram_bound(index: usize) -> u32 {
    if index == ES_HIST_BINS - 1 {
        return u32::MAX;
    }
    let power = 1u64 << (index / 2);
    let bound = power + ((index % 2) as u64) * (power / 2);
    if bound > u32::MAX as u64 {
        u32::MAX
    } else {
        bound as u32
    }
}

const fn histogram_bin(value: u32) -> usize {
    let mut i = 0;
    while i < ES_HIST_BINS {
        if value <= histogram_bound(i) {
            return i;
        }
        i += 1;
    }
    ES_HIST_BINS - 1
}
