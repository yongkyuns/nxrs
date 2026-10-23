//! Bounded test-only CSI capture bookkeeping. No device access or classifier.
#![forbid(unsafe_code)]

pub mod echo;
pub mod handoff;
pub mod lifecycle;
pub mod traffic;

pub const MAX_CSI: usize = 612;
pub const MAX_RX_CTRL: usize = 64;
pub const QUEUE_SIZE: usize = 8;

/// Private fixture ABI, filled mechanically from the pinned SDK headers.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Metadata {
    pub timestamp_us: u32,
    pub rx_ctrl_len: u16,
    pub len: u16,
    pub mac: [u8; 6],
    pub rssi: i8,
    pub noise_floor: i8,
    pub channel: u8,
    pub secondary_channel: u8,
    pub sig_mode: u8,
    pub bandwidth: u8,
    pub mcs: u8,
    pub stbc: u8,
    pub first_word_invalid: u8,
    pub agc_gain: u8,
    pub fft_gain: i8,
    pub reserved: [u8; 3],
    pub rx_ctrl: [u8; MAX_RX_CTRL],
}

const _: () = {
    assert!(std::mem::size_of::<Metadata>() == 92);
    assert!(std::mem::offset_of!(Metadata, rx_ctrl) == 28);
};

#[derive(Clone, Copy)]
pub struct Frame {
    pub metadata: Metadata,
    pub received_at: std::time::Instant,
    pub data: [i8; MAX_CSI],
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct Progress {
    pub frames: u64,
    pub duplicate_timestamps: u64,
    pub stale_timestamps: u64,
    pub identity_changes: u64,
    pub first_word_invalid: u64,
    last: Option<(u32, u8, u8)>,
}
impl Progress {
    /// The hardware clock is wrapping u32 microseconds, not host report time.
    pub fn observe(&mut self, m: &Metadata) -> bool {
        if let Some((last, channel, secondary)) = self.last {
            if m.channel != channel || m.secondary_channel != secondary {
                self.identity_changes += 1;
                return false;
            }
            let elapsed = m.timestamp_us.wrapping_sub(last);
            if elapsed == 0 {
                self.duplicate_timestamps += 1;
                return false;
            }
            if elapsed >= 0x8000_0000 {
                self.stale_timestamps += 1;
                return false;
            }
        }
        self.last = Some((m.timestamp_us, m.channel, m.secondary_channel));
        self.frames += 1;
        self.first_word_invalid += u64::from(m.first_word_invalid != 0);
        true
    }
}

/// Scalar diagnostics include rejected peers without retaining their identities.
#[derive(Clone, Copy, Debug, Default)]
pub struct Observation {
    pub callbacks: u64,
    pub foreign: u64,
    pub legacy: u64,
    pub ht: u64,
    pub other: u64,
    pub last: Option<(std::time::Instant, u8, u8, u8, u16, bool)>,
}

/// One session at a time; old SDK callbacks cannot enter a new generation.
pub struct Queue {
    epoch: u32,
    peer: [u8; 6],
    // Legacy FIFO bookkeeping is exercised only by the core tests. The native
    // handoff uses admit() and the SPSC ring, without a second frame buffer.
    #[cfg(test)]
    frames: [Option<Frame>; QUEUE_SIZE],
    #[cfg(test)]
    head: usize,
    #[cfg(test)]
    len: usize,
    pub overflow: u64,
    pub malformed: u64,
    pub observation: Observation,
}
impl Default for Queue {
    fn default() -> Self {
        Self::new()
    }
}
impl Queue {
    pub const fn new() -> Self {
        Self {
            epoch: 0,
            peer: [0; 6],
            #[cfg(test)]
            frames: [None; QUEUE_SIZE],
            #[cfg(test)]
            head: 0,
            #[cfg(test)]
            len: 0,
            overflow: 0,
            malformed: 0,
            observation: Observation {
                callbacks: 0,
                foreign: 0,
                legacy: 0,
                ht: 0,
                other: 0,
                last: None,
            },
        }
    }
    pub fn begin(&mut self, epoch: u32, peer: [u8; 6]) -> Result<(), &'static str> {
        if epoch == 0 || self.epoch != 0 {
            return Err("capture already active or invalid epoch");
        }
        *self = Self::new();
        self.epoch = epoch;
        self.peer = peer;
        Ok(())
    }
    pub fn end(&mut self) {
        self.epoch = 0;
    }
    pub fn admit(&mut self, epoch: u32, metadata: Metadata, bytes: &[i8]) -> Option<Frame> {
        if epoch == 0 || epoch != self.epoch {
            return None;
        }
        // Observe rejected peers too, without retaining their identity or payload.
        // Diagnostics never renew accepted RF freshness or change admission.
        let matching = metadata.mac == self.peer;
        self.observation.callbacks += 1;
        match metadata.sig_mode {
            0 => self.observation.legacy += 1,
            1 => self.observation.ht += 1,
            _ => self.observation.other += 1,
        }
        let received_at = std::time::Instant::now();
        self.observation.last = Some((
            received_at,
            metadata.sig_mode,
            metadata.bandwidth,
            metadata.channel,
            metadata.len,
            matching,
        ));
        if !matching {
            self.observation.foreign += 1;
            return None;
        }
        if bytes.is_empty()
            || bytes.len() > MAX_CSI
            || bytes.len() != usize::from(metadata.len)
            || !bytes.len().is_multiple_of(2)
            || usize::from(metadata.rx_ctrl_len) > MAX_RX_CTRL
            || metadata.rx_ctrl_len < 24
            || metadata.channel == 0
            || metadata.channel > 14
            || metadata.first_word_invalid > 1
            || metadata.first_word_invalid != 0 && bytes.len() < 4
        {
            self.malformed += 1;
            return None;
        }
        let mut data = [0; MAX_CSI];
        data[..bytes.len()].copy_from_slice(bytes);
        Some(Frame {
            metadata,
            received_at,
            data,
        })
    }
    #[cfg(test)]
    pub fn push(&mut self, epoch: u32, metadata: Metadata, bytes: &[i8]) {
        let Some(frame) = self.admit(epoch, metadata, bytes) else {
            return;
        };
        if self.len == QUEUE_SIZE {
            self.overflow += 1;
            return;
        }
        self.frames[(self.head + self.len) % QUEUE_SIZE] = Some(frame);
        self.len += 1;
    }
    #[cfg(test)]
    pub fn pop(&mut self) -> Option<Frame> {
        if self.len == 0 {
            return None;
        }
        let result = self.frames[self.head].take();
        self.head = (self.head + 1) % QUEUE_SIZE;
        self.len -= 1;
        result
    }
}

pub fn peer(text: &str) -> Result<[u8; 6], &'static str> {
    let mut out = [0; 6];
    let mut parts = text.split(':');
    for byte in &mut out {
        let part = parts.next().ok_or("expected six MAC octets")?;
        if part.len() != 2 {
            return Err("expected two hex digits per octet");
        }
        *byte = u8::from_str_radix(part, 16).map_err(|_| "invalid MAC octet")?;
    }
    if parts.next().is_some() || out == [0; 6] || out[0] & 1 != 0 {
        return Err("expected unicast AP BSSID");
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    pub(super) fn metadata() -> Metadata {
        Metadata {
            timestamp_us: 100,
            rx_ctrl_len: 28,
            len: 128,
            mac: [2, 0, 0, 0, 0, 1],
            rssi: -50,
            noise_floor: -90,
            channel: 6,
            secondary_channel: 0,
            sig_mode: 0,
            bandwidth: 0,
            mcs: 0,
            stbc: 0,
            first_word_invalid: 0,
            agc_gain: 20,
            fft_gain: -3,
            reserved: [0; 3],
            rx_ctrl: [0; MAX_RX_CTRL],
        }
    }
    #[test]
    fn bounded_overflow_preserves_existing_frames() {
        let m = metadata();
        let mut q = Queue::new();
        q.begin(1, m.mac).unwrap();
        for i in 0..QUEUE_SIZE {
            q.push(1, m, &[i as i8; 128]);
        }
        q.push(1, m, &[99; 128]);
        assert_eq!(q.overflow, 1);
        for i in 0..QUEUE_SIZE {
            assert_eq!(q.pop().unwrap().data[0], i as i8);
        }
        assert!(q.pop().is_none());
    }
    #[test]
    fn late_and_foreign_callbacks_are_isolated() {
        let m = metadata();
        let mut q = Queue::new();
        q.begin(1, m.mac).unwrap();
        q.end();
        q.push(1, m, &[1; 128]);
        assert!(q.pop().is_none());
        assert_eq!(q.observation.callbacks, 0);
        q.begin(2, m.mac).unwrap();
        q.push(1, m, &[1; 128]);
        let mut foreign = m;
        foreign.mac[5] = 2;
        q.push(2, foreign, &[1; 128]);
        assert!(q.pop().is_none());
        assert_eq!(q.observation.callbacks, 1);
        assert_eq!(q.observation.foreign, 1);
        q.push(2, m, &[2; 128]);
        assert_eq!(q.pop().unwrap().data[0], 2);
    }
    #[test]
    fn diagnostics_distinguish_peer_and_phy_without_changing_admission() {
        let mut q = Queue::new();
        let mut m = metadata();
        q.begin(1, m.mac).unwrap();
        q.push(1, m, &[0; 128]);
        m.mac[5] = 2;
        m.sig_mode = 1;
        q.push(1, m, &[0; 128]);
        m.sig_mode = 3;
        q.push(1, m, &[0; 128]);
        assert_eq!(q.observation.callbacks, 3);
        assert_eq!(q.observation.foreign, 2);
        assert_eq!(
            (q.observation.legacy, q.observation.ht, q.observation.other),
            (1, 1, 1)
        );
        let (_, mode, width, channel, length, matching) = q.observation.last.unwrap();
        assert_eq!(
            (mode, width, channel, length, matching),
            (3, 0, 6, 128, false)
        );
        assert!(q.pop().is_some());
        assert!(q.pop().is_none());
        q.end();
        q.push(1, m, &[0; 128]);
        assert_eq!(q.observation.callbacks, 3);
        q.begin(2, metadata().mac).unwrap();
        assert_eq!(q.observation.callbacks, 0);
        assert!(q.observation.last.is_none());
    }
    #[test]
    fn malformed_packets_do_not_enter_queue() {
        let m = metadata();
        let mut q = Queue::new();
        q.begin(1, m.mac).unwrap();
        q.push(1, m, &[]);
        q.push(1, m, &[0; 126]);
        let mut bad = m;
        bad.rx_ctrl_len = 65;
        q.push(1, bad, &[0; 128]);
        bad = m;
        bad.channel = 0;
        q.push(1, bad, &[0; 128]);
        assert_eq!(q.malformed, 4);
        assert!(q.pop().is_none());
    }
    #[test]
    fn overlapping_sessions_rejected() {
        let mut q = Queue::new();
        assert!(q.begin(0, [2; 6]).is_err());
        q.begin(1, [2; 6]).unwrap();
        assert!(q.begin(2, [2; 6]).is_err());
    }
    #[test]
    fn timestamps_wrap_and_stalls_do_not_refresh() {
        let mut p = Progress::default();
        let mut m = metadata();
        m.timestamp_us = u32::MAX - 10;
        assert!(p.observe(&m));
        m.timestamp_us = 20;
        assert!(p.observe(&m));
        assert!(!p.observe(&m));
        m.timestamp_us = 19;
        assert!(!p.observe(&m));
        m.timestamp_us = 21;
        assert!(p.observe(&m));
        assert_eq!(p.frames, 3);
        assert_eq!(p.duplicate_timestamps, 1);
        assert_eq!(p.stale_timestamps, 1);
    }
    #[test]
    fn channel_changes_invalidate_window() {
        let mut p = Progress::default();
        let mut m = metadata();
        assert!(p.observe(&m));
        m.timestamp_us += 1;
        m.channel = 11;
        assert!(!p.observe(&m));
        assert_eq!(p.identity_changes, 1);
        assert_eq!(p.frames, 1);
    }
    #[test]
    fn explicit_unicast_peer_required() {
        assert_eq!(peer("02:00:00:00:00:01").unwrap(), [2, 0, 0, 0, 0, 1]);
        for s in [
            "",
            "00:00:00:00:00:00",
            "ff:ff:ff:ff:ff:ff",
            "02:0:00:00:00:01",
            "02:00:00:00:00:01:02",
        ] {
            assert!(peer(s).is_err());
        }
    }
    #[test]
    fn metadata_scalar_abi() {
        assert_eq!(std::mem::size_of::<Metadata>(), 92);
        assert_eq!(std::mem::align_of::<Metadata>(), 4);
    }
}
