//! Versioned microphone parts for the camera's authenticated multipart stream.
#![forbid(unsafe_code)]

pub const AV_PATH: &str = "/stream?av=1";
pub const AV_HEADER: &str = "x-nxrs-av";
pub const AV_VERSION: &str = "1";
pub const AUDIO_TYPE: &str = "application/vnd.nxrs.pcm16.v1";
pub const SAMPLE_RATE_HZ: u32 = 16_000;
pub const BLOCK_SAMPLES: u64 = 320;
pub const BLOCK_BYTES: usize = BLOCK_SAMPLES as usize * 2;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct AudioStamp {
    /// Sample offset on the capture timeline. Gaps indicate lost blocks.
    pub first_sample: u64,
    /// Camera monotonic time in the same domain as the JPEG header.
    pub capture_ms: u64,
}

impl AudioStamp {
    pub fn end_sample(self) -> Option<u64> {
        if self.first_sample.is_multiple_of(BLOCK_SAMPLES) {
            self.first_sample.checked_add(BLOCK_SAMPLES)
        } else {
            None
        }
    }

    pub fn follows(self, previous: Self) -> bool {
        previous
            .end_sample()
            .is_some_and(|end| self.first_sample >= end)
            && self.capture_ms >= previous.capture_ms
    }
}

#[derive(Clone, Copy)]
pub struct AudioBlock<'a> {
    pub stamp: AudioStamp,
    /// One complete block of signed little-endian mono PCM.
    pub pcm: &'a [u8; BLOCK_BYTES],
}
