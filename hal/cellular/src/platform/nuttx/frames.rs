//! Keep the pinned PPP engine within its control-packet parser's limits.
use crate::Error;

pub const MTU: usize = 1500;
pub const MAX_ENCODED: usize = (MTU + 6) * 2 + 2;
const CONTROL_BYTES: usize = 64;

pub struct Frames {
    encoded: [u8; MAX_ENCODED],
    len: usize,
    overflow: bool,
}

impl Default for Frames {
    fn default() -> Self {
        let mut encoded = [0; MAX_ENCODED];
        encoded[0] = 0x7e;
        Self {
            encoded,
            len: 1,
            overflow: false,
        }
    }
}

impl Frames {
    pub fn push(&mut self, byte: u8) -> Result<Option<&[u8]>, Error> {
        if byte == 0x7e {
            let len = self.len;
            self.len = 1;
            if std::mem::take(&mut self.overflow) || !admitted(&self.encoded[1..len]) {
                return Ok(None);
            }
            self.encoded[len] = byte;
            return Ok(Some(&self.encoded[..=len]));
        }
        if self.len == self.encoded.len() - 1 {
            self.overflow = true;
        }
        if !self.overflow {
            self.encoded[self.len] = byte;
            self.len += 1;
            // Between PPP frames (or before the first flag), the modem
            // can report a transition out of data mode in plain text.
            let frame = &self.encoded[1..self.len];
            if frame.first() != Some(&0xff) && frame.ends_with(b"\r\nNO CARRIER\r\n") {
                return Err(Error::LinkLost);
            }
        }
        Ok(None)
    }
}

fn inspect(raw: &[u8]) -> Option<([u8; CONTROL_BYTES], usize)> {
    if raw.first() != Some(&0xff) {
        return None;
    }
    let mut header = [0; CONTROL_BYTES];
    let mut escaped = false;
    let mut len = 0;
    for &byte in raw {
        if byte == 0x7d && !escaped {
            escaped = true;
            continue;
        }
        if let Some(slot) = header.get_mut(len) {
            *slot = if escaped { byte ^ 0x20 } else { byte };
        }
        escaped = false;
        len += 1;
    }
    if escaped || len < 6 || header[1] != 3 {
        return None;
    }
    Some((header, len))
}

/// The last Configure-Request or Authenticate-Request in an engine burst.
pub fn last_request(burst: &[u8]) -> Option<&[u8]> {
    let mut start = None;
    let mut request = None;
    for (end, &byte) in burst.iter().enumerate() {
        if byte != 0x7e {
            continue;
        }
        let Some(begin) = start.replace(end) else {
            continue;
        };
        if let Some((bytes, _)) = inspect(&burst[begin + 1..end]) {
            if matches!(bytes[2..5], [0xc0, 0x21 | 0x23, 1] | [0x80, 0x21, 1]) {
                request = Some(&burst[begin..=end]);
            }
        }
    }
    request
}

fn admitted(raw: &[u8]) -> bool {
    let Some((header, len)) = inspect(raw) else {
        return false;
    };
    match [header[2], header[3]] {
        [0x00, 0x21] => (26..=MTU + 6).contains(&len),
        [0xc0, 0x21 | 0x23] | [0x80, 0x21] => control(&header, len),
        // Permit the engine to reject unknown small protocol requests within
        // its control response storage bound. Larger ones are dropped.
        _ => len <= CONTROL_BYTES,
    }
}

fn control(header: &[u8; CONTROL_BYTES], len: usize) -> bool {
    let size = u16::from_be_bytes([header[6], header[7]]) as usize;
    if len > header.len() || size < 4 || size + 6 > len {
        return false;
    }
    if header[3] == 0x23 || !matches!(header[4], 1..=4) {
        return true;
    }
    let mut options = &header[8..size + 4];
    for _ in 0..6 {
        if options.is_empty() {
            return true;
        }
        let count = options.get(1).copied().unwrap_or_default() as usize;
        if !(2..=6).contains(&count) || count > options.len() {
            return false;
        }
        options = &options[count..];
    }
    options.is_empty()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn control_sizes_fail_closed_before_the_ppp_parser() {
        let lcp = [0xff, 3, 0xc0, 0x21, 3, 1, 0, 10, 2, 6, 0, 0, 0, 0, 0, 0];
        assert!(admitted(&lcp));
        let mut bad_size = lcp;
        bad_size[7] = 16;
        assert!(!admitted(&bad_size));
        let mut bad_option = lcp;
        bad_option[9] = 1;
        assert!(!admitted(&bad_option));
        assert!(!admitted(&lcp[..4]));
    }

    #[test]
    fn retransmission_selects_the_request_and_leaves_peer_acknowledgments_out() {
        let request = [0x7e, 0xff, 0x7d, 0x23, 0x80, 0x21, 0x7d, 0x21, 0, 0, 0x7e];
        let ack = [0x7e, 0xff, 3, 0x80, 0x21, 2, 0, 0, 0x7e];
        let burst = [request.as_slice(), ack.as_slice()].concat();
        assert_eq!(last_request(&burst), Some(request.as_slice()));
        assert_eq!(last_request(&ack), None);
    }

    #[test]
    fn detects_modem_hangup_between_frames() {
        let mut frames = Frames::default();
        assert_eq!(frames.push(0x7e), Ok(None));
        for &byte in b"\r\nNO CARRIER\r" {
            assert_eq!(frames.push(byte), Ok(None));
        }
        assert_eq!(frames.push(b'\n'), Err(Error::LinkLost));
    }
}
