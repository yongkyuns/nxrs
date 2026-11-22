//! Latest-value publication over a nonblocking byte stream.
use std::io::{self, Write};
use std::time::Instant;

struct Pending<Frame> {
    frame: Frame,
    sent: usize,
    deadline: Instant,
}

/// Retains one in-flight frame and one waiting sample. A newer sample replaces
/// the waiting sample. Partial writes retain the same bytes until completion.
pub struct Publisher<Sample, Frame> {
    latest: Option<Sample>,
    pending: Option<Pending<Frame>>,
}

impl<Sample, Frame> Default for Publisher<Sample, Frame> {
    fn default() -> Self {
        Self {
            latest: None,
            pending: None,
        }
    }
}

impl<Sample, Frame: AsRef<[u8]>> Publisher<Sample, Frame> {
    pub fn offer(&mut self, sample: Sample) {
        self.latest = Some(sample);
    }

    pub fn is_idle(&self) -> bool {
        self.pending.is_none() && self.latest.is_none()
    }

    pub fn in_flight(&self) -> bool {
        self.pending.is_some()
    }

    /// Drop the interrupted frame after retiring the stream. Keep any newer
    /// sample waiting for the replacement connection.
    pub fn reset_stream(&mut self) {
        self.pending = None;
    }

    /// Attempt at most one write and one flush. The deadline is captured when
    /// a sample becomes a frame. A hard error requires replacing the stream.
    pub fn service(
        &mut self,
        output: &mut impl Write,
        now: Instant,
        deadline: Instant,
        encode: impl FnOnce(&Sample) -> io::Result<Frame>,
    ) -> io::Result<bool> {
        if self.pending.is_none() {
            let Some(sample) = self.latest.take() else {
                return Ok(false);
            };
            self.pending = Some(Pending {
                frame: encode(&sample)?,
                sent: 0,
                deadline,
            });
        }
        let pending = self.pending.as_mut().unwrap();
        if now >= pending.deadline {
            return Err(io::ErrorKind::TimedOut.into());
        }
        let bytes = pending.frame.as_ref();
        if pending.sent < bytes.len() {
            match output.write(&bytes[pending.sent..]) {
                Ok(0) => return Err(io::ErrorKind::WriteZero.into()),
                Ok(count) => pending.sent += count,
                Err(error) if blocked(&error) => return Ok(false),
                Err(error) => return Err(error),
            }
        }
        if pending.sent < bytes.len() {
            return Ok(false);
        }
        match output.flush() {
            Ok(()) => {
                self.pending = None;
                Ok(true)
            }
            Err(error) if blocked(&error) => Ok(false),
            Err(error) => Err(error),
        }
    }
}

fn blocked(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[derive(Default)]
    struct Peer {
        received: Vec<u8>,
        write_blocked: bool,
        flush_blocked: bool,
    }

    impl Write for Peer {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            if self.write_blocked {
                return Err(io::ErrorKind::WouldBlock.into());
            }
            let count = bytes.len().min(2);
            self.received.extend_from_slice(&bytes[..count]);
            Ok(count)
        }

        fn flush(&mut self) -> io::Result<()> {
            if self.flush_blocked {
                return Err(io::ErrorKind::WouldBlock.into());
            }
            Ok(())
        }
    }

    #[test]
    fn partial_frames_stay_ordered_and_waiting_samples_coalesce() {
        let now = Instant::now();
        let deadline = now + Duration::from_secs(1);
        let mut publisher = Publisher::default();
        let mut peer = Peer {
            flush_blocked: true,
            ..Peer::default()
        };
        let encode = |sample: &&'static [u8]| Ok(*sample);

        publisher.offer(b"abc" as &[u8]);
        assert!(!publisher.service(&mut peer, now, deadline, encode).unwrap());
        publisher.offer(b"old");
        publisher.offer(b"new");
        peer.write_blocked = true;
        assert!(!publisher.service(&mut peer, now, deadline, encode).unwrap());
        assert_eq!(peer.received, b"ab");
        peer.write_blocked = false;
        assert!(!publisher.service(&mut peer, now, deadline, encode).unwrap());
        assert_eq!(peer.received, b"abc");
        peer.flush_blocked = false;
        assert!(publisher.service(&mut peer, now, deadline, encode).unwrap());
        assert!(!publisher.service(&mut peer, now, deadline, encode).unwrap());
        assert!(publisher.service(&mut peer, now, deadline, encode).unwrap());
        assert!(publisher.is_idle());
        assert_eq!(peer.received, b"abcnew");
    }

    #[test]
    fn timeout_keeps_newest_sample_for_the_replacement_connection() {
        let now = Instant::now();
        let deadline = now + Duration::from_secs(1);
        let mut publisher = Publisher::default();
        let mut stalled = Peer::default();
        let encode = |sample: &&'static [u8]| Ok(*sample);

        publisher.offer(b"abc" as &[u8]);
        assert!(!publisher
            .service(&mut stalled, now, deadline, encode)
            .unwrap());
        publisher.offer(b"xy");
        let later = deadline + Duration::from_secs(1);
        assert_eq!(
            publisher
                .service(&mut stalled, deadline, later, encode)
                .unwrap_err()
                .kind(),
            io::ErrorKind::TimedOut
        );
        publisher.reset_stream();
        let mut replacement = Peer::default();
        assert!(publisher
            .service(&mut replacement, deadline, later, encode)
            .unwrap());
        assert_eq!(stalled.received, b"ab");
        assert_eq!(replacement.received, b"xy");
    }
}
