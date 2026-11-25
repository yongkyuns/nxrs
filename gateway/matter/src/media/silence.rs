use super::AudioPacket;
use bytes::{Bytes, BytesMut};

impl AudioPacket {
    /// Replace microphone data with encoded digital silence of the same duration.
    /// The four mono wideband CELT frames are libopus encodings of zero PCM at
    /// 16 kHz. RFC 6716 sections 3.1 and 3.2 describe packet duration and framing.
    pub fn silence(&self) -> Option<Bytes> {
        let toc = *self.opus.first()?;
        // Units of 2.5 ms at the 48 kHz RTP clock.
        let size = (toc >> 3) & 3;
        let duration: u8 = if toc & 0x80 != 0 {
            1 << size
        } else if toc & 0x60 == 0x60 {
            4 << (size & 1)
        } else {
            [4, 8, 16, 24][size as usize]
        };
        let count = match toc & 3 {
            0 => 1,
            1 | 2 => 2,
            _ => self.opus.get(1)? & 63,
        };
        let units = duration.checked_mul(count)?;
        if !(1..=48).contains(&units) {
            return None;
        }
        let shift = units.trailing_zeros().min(3);
        let frames = units >> shift;
        const SINGLE: [&[u8]; 4] = [
            &[0xa0, 0xff, 0xfe],
            &[0xa8, 0xff, 0xfe],
            &[0xb0, 0xff, 0xfe],
            &[0xb8, 0xff, 0xfe],
        ];
        let one = SINGLE[shift as usize];
        if frames == 1 {
            return Some(Bytes::from_static(one));
        }
        let mut out = BytesMut::with_capacity(2 + 2 * usize::from(frames));
        out.extend_from_slice(&[one[0] | 3, frames]);
        for _ in 0..frames {
            out.extend_from_slice(&one[1..]);
        }
        Some(out.freeze())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn replace(payload: &[u8]) -> Option<Bytes> {
        AudioPacket {
            pts_48khz: 0,
            source_time: std::time::Instant::now(),
            opus: Bytes::copy_from_slice(payload),
        }
        .silence()
    }
    #[test]
    fn microphone_silence_preserves_duration_across_opus_modes_and_packet_counts() {
        assert_eq!(replace(&[0x08, 1]).unwrap().as_ref(), &[0xb8, 0xff, 0xfe]);
        assert_eq!(replace(&[0x60, 1]).unwrap().as_ref(), &[0xb0, 0xff, 0xfe]);
        assert_eq!(
            replace(&[0x83, 3, 1]).unwrap().as_ref(),
            &[0xa3, 3, 0xff, 0xfe, 0xff, 0xfe, 0xff, 0xfe]
        );
        assert!(replace(&[0x1b, 3]).is_none());
        assert!(replace(&[0x83, 0]).is_none());
    }
}
