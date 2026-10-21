//! Inspection runtime policy; timed qualification remains separately bounded.
#[derive(Debug, PartialEq, Eq)]
pub enum Duration {
    Timed(u64),
    Continuous,
}

impl Duration {
    pub fn parse(value: &str, reconnect: u64) -> Result<Self, &'static str> {
        if value == "continuous" {
            return if reconnect == 0 {
                Ok(Self::Continuous)
            } else {
                Err("continuous mode requires reconnect=0")
            };
        }
        let seconds: u64 = value.parse().map_err(|_| "duration")?;
        if !(10..=3600).contains(&seconds) || reconnect >= seconds {
            return Err("invalid duration/reconnect time");
        }
        Ok(Self::Timed(seconds))
    }

    pub fn expired(&self, elapsed_ms: u64) -> bool {
        matches!(self, Self::Timed(seconds) if elapsed_ms >= seconds * 1000)
    }
}

#[derive(Default)]
pub struct Control {
    line: [u8; 8],
    used: usize,
    overflow: bool,
}

impl Control {
    pub fn input(&mut self, bytes: &[u8]) -> bool {
        for &byte in bytes {
            if byte == b'\n' {
                let stop = !self.overflow
                    && self.line[..self.used]
                        .strip_suffix(b"\r")
                        .unwrap_or(&self.line[..self.used])
                        == b"stop";
                self.used = 0;
                self.overflow = false;
                if stop {
                    return true;
                }
            } else if self.used < self.line.len() {
                self.line[self.used] = byte;
                self.used += 1;
            } else {
                self.overflow = true;
            }
        }
        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn timed_bounds_and_injected_reconnect_are_preserved() {
        for value in ["0", "9", "3601", "invalid"] {
            assert!(Duration::parse(value, 0).is_err());
        }
        assert!(Duration::parse("60", 60).is_err());
        assert_eq!(Duration::parse("60", 30), Ok(Duration::Timed(60)));
        assert!(!Duration::Timed(60).expired(59_999));
        assert!(Duration::Timed(60).expired(60_000));
    }
    #[test]
    fn continuous_mode_has_no_hourly_expiry_or_injected_disconnect() {
        assert_eq!(Duration::parse("continuous", 0), Ok(Duration::Continuous));
        assert!(Duration::parse("continuous", 1).is_err());
        assert!(!Duration::Continuous.expired(3_600_000));
        assert!(!Duration::Continuous.expired(u64::MAX));
    }
    #[test]
    fn stop_is_an_exact_complete_bounded_line() {
        let mut control = Control::default();
        assert!(!control.input(b"sto"));
        assert!(control.input(b"p\r\n"));
        assert!(!control.input(b"please stop\n"));
        assert!(!control.input(b"xxxxxxxxxxxxxxxxxxxxxxxxstop\n"));
        assert!(control.input(b"stop\n"));
    }
}
