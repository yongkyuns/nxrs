//! Versioned microphone parts for the camera's authenticated multipart stream.

pub const AV_PATH: &str = "/stream?av=1";
pub const AV_HEADER: &str = "x-nxrs-av";
pub const AV_VERSION: &str = "1";
pub const CONTROLLED_AV_PATH: &str = "/stream?av=2";
pub const CONTROLLED_AV_VERSION: &str = "2";
pub const MICROPHONE_STATE_TYPE: &str = "application/vnd.nxrs.microphone-state.v2";
pub const AUDIO_TYPE: &str = "application/vnd.nxrs.pcm16.v1";
pub const SAMPLE_RATE_HZ: u32 = 16_000;
pub const BLOCK_SAMPLES: u64 = 320;
pub const BLOCK_BYTES: usize = BLOCK_SAMPLES as usize * 2;

/// One command on the authenticated A/V transport, or the acknowledgement
/// sent in its multipart response after the change takes effect.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct MicrophoneState {
    pub sequence: core::num::NonZeroU64,
    pub muted: bool,
}

impl MicrophoneState {
    pub const BYTES: usize = 9;

    pub fn encode(self) -> [u8; Self::BYTES] {
        let mut bytes = [0; Self::BYTES];
        bytes[..8].copy_from_slice(&self.sequence.get().to_be_bytes());
        bytes[8] = u8::from(self.muted);
        bytes
    }

    pub fn decode(bytes: &[u8; Self::BYTES]) -> Option<Self> {
        let sequence = core::num::NonZeroU64::new(u64::from_be_bytes(bytes[..8].try_into().ok()?))?;
        let muted = match bytes[8] {
            0 => false,
            1 => true,
            _ => return None,
        };
        Some(Self { sequence, muted })
    }
}

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
