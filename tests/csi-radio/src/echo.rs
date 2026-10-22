//! Bounded qualification traffic. A missing reply must not suppress fresh RF stimulus.
use std::time::Duration;

pub const CAPACITY: usize = 32;
const TAG: &[u8; 12] = b"NXRS-CSIRAD!";
const DEADLINE: Duration = Duration::from_secs(2);

#[derive(Debug, PartialEq, Eq)]
pub enum Error {
    Full,
    InvalidReply,
}

pub struct Window {
    pending: [Option<(u32, Duration)>; CAPACITY],
    pub sent: u32,
    pub echoed: u32,
}
impl Default for Window {
    fn default() -> Self {
        Self::new()
    }
}
impl Window {
    pub const fn new() -> Self {
        Self {
            pending: [None; CAPACITY],
            sent: 0,
            echoed: 0,
        }
    }
    pub fn send(&mut self, now: Duration) -> Result<[u8; 16], Error> {
        let sequence = self.sent;
        let slot = &mut self.pending[sequence as usize % CAPACITY];
        if slot.is_some() || sequence == u32::MAX {
            return Err(Error::Full);
        }
        let mut packet = [0; 16];
        packet[..4].copy_from_slice(&sequence.to_be_bytes());
        packet[4..].copy_from_slice(TAG);
        *slot = Some((sequence, now));
        self.sent += 1;
        Ok(packet)
    }
    pub fn receive(&mut self, packet: &[u8]) -> Result<(), Error> {
        if packet.len() != 16 || &packet[4..] != TAG {
            return Err(Error::InvalidReply);
        }
        let sequence = u32::from_be_bytes(packet[..4].try_into().unwrap());
        let slot = &mut self.pending[sequence as usize % CAPACITY];
        if !matches!(*slot, Some((expected, _)) if expected == sequence) {
            return Err(Error::InvalidReply);
        }
        *slot = None;
        self.echoed += 1;
        Ok(())
    }
    pub fn expired(&self, now: Duration) -> Option<u32> {
        self.pending
            .iter()
            .flatten()
            .filter(|(_, at)| now.saturating_sub(*at) >= DEADLINE)
            .map(|(sequence, _)| *sequence)
            .min()
    }
    pub fn pending(&self) -> bool {
        self.sent != self.echoed
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn one_lost_echo_does_not_stop_stimulus_or_relax_deadline() {
        let mut w = Window::new();
        for i in 0..20 {
            let packet = w.send(Duration::from_millis(i * 100)).unwrap();
            if i != 0 {
                w.receive(&packet).unwrap();
            }
        }
        assert_eq!((w.sent, w.echoed), (20, 19));
        assert!(w.pending());
        assert_eq!(w.expired(Duration::from_millis(1999)), None);
        assert_eq!(w.expired(Duration::from_millis(2000)), Some(0));
    }
    #[test]
    fn reordering_is_allowed_but_duplicates_malformed_and_unsent_are_rejected() {
        let mut w = Window::new();
        let a = w.send(Duration::ZERO).unwrap();
        let b = w.send(Duration::ZERO).unwrap();
        w.receive(&b).unwrap();
        assert_eq!(w.receive(&b), Err(Error::InvalidReply));
        let mut bad = a;
        bad[15] ^= 1;
        assert_eq!(w.receive(&bad), Err(Error::InvalidReply));
        assert_eq!(w.receive(&a[..15]), Err(Error::InvalidReply));
        bad = a;
        bad[..4].copy_from_slice(&32u32.to_be_bytes());
        assert_eq!(w.receive(&bad), Err(Error::InvalidReply));
        assert_eq!((w.sent, w.echoed), (2, 1));
        w.receive(&a).unwrap();
        assert!(!w.pending());
    }
    #[test]
    fn full_window_cannot_overwrite_missing_reply_across_wrap() {
        let mut w = Window::new();
        let a = w.send(Duration::ZERO).unwrap();
        for _ in 1..CAPACITY {
            let packet = w.send(Duration::ZERO).unwrap();
            w.receive(&packet).unwrap();
        }
        assert_eq!(w.send(Duration::ZERO), Err(Error::Full));
        assert_eq!(w.expired(DEADLINE), Some(0));
        w.receive(&a).unwrap();
        let wrapped = w.send(DEADLINE).unwrap();
        assert_eq!(w.receive(&a), Err(Error::InvalidReply));
        w.receive(&wrapped).unwrap();
        assert!(!w.pending());
    }
}
