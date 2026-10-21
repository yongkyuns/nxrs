//! Portable record bytes; provider validation and I/O policy stay with the writer.
#![forbid(unsafe_code)]

use ::storage::{Capture, PixelFormat};

pub(crate) const HEADER_BYTES: usize = 40;
pub(crate) const MAGIC: &[u8; 8] = b"RCAMREC1";

pub(crate) fn encode_header(
    capture: Capture,
    sequence: u64,
    payload_len: usize,
) -> [u8; HEADER_BYTES] {
    let mut header = [0; HEADER_BYTES];
    header[..8].copy_from_slice(MAGIC);
    header[8..10].copy_from_slice(&capture.format.width.to_le_bytes());
    header[10..12].copy_from_slice(&capture.format.height.to_le_bytes());
    header[12] = match capture.format.pixels {
        PixelFormat::Gray8 => 0,
        PixelFormat::Rgb565 => 1,
        PixelFormat::Jpeg => 2,
    };
    header[16..24].copy_from_slice(&sequence.to_le_bytes());
    header[24..32].copy_from_slice(&capture.timestamp_ms.to_le_bytes());
    header[32..40].copy_from_slice(&(payload_len as u64).to_le_bytes());
    header
}

#[cfg(test)]
mod tests {
    use super::*;
    use ::storage::Format;

    fn capture(pixels: PixelFormat) -> Capture {
        Capture {
            format: Format {
                width: 0x1234,
                height: 0x5678,
                pixels,
            },
            len: 0,
            timestamp_ms: 0x1112_1314_1516_1718,
        }
    }

    #[test]
    fn header_matches_rcamrec1_golden_bytes() {
        assert_eq!(
            encode_header(
                capture(PixelFormat::Gray8),
                0x0102_0304_0506_0708,
                0x0102_0304
            ),
            [
                0x52, 0x43, 0x41, 0x4d, 0x52, 0x45, 0x43, 0x31, 0x34, 0x12, 0x78, 0x56, 0x00, 0x00,
                0x00, 0x00, 0x08, 0x07, 0x06, 0x05, 0x04, 0x03, 0x02, 0x01, 0x18, 0x17, 0x16, 0x15,
                0x14, 0x13, 0x12, 0x11, 0x04, 0x03, 0x02, 0x01, 0x00, 0x00, 0x00, 0x00,
            ]
        );
    }

    #[test]
    fn pixel_tags_match_the_golden_values() {
        for (pixels, expected) in [
            (PixelFormat::Gray8, 0),
            (PixelFormat::Rgb565, 1),
            (PixelFormat::Jpeg, 2),
        ] {
            assert_eq!(encode_header(capture(pixels), 1, 0)[12], expected);
        }
    }

    #[test]
    fn payload_length_is_written_as_little_endian_u64() {
        let header = encode_header(capture(PixelFormat::Jpeg), 1, 0x0102_0304);
        assert_eq!(
            &header[32..40],
            &[0x04, 0x03, 0x02, 0x01, 0x00, 0x00, 0x00, 0x00]
        );
    }
}
