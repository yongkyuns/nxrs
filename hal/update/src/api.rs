//! Authenticated secondary-slot staging. This API never selects or confirms a boot.
#![forbid(unsafe_code)]
use core::num::NonZeroU32;
use firmware_image::{Layout, Trust, Version};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Policy,
    Geometry,
    Missing,
    Unsupported,
    Busy,
    Authentication,
    BootBusy,
    Io,
    Readback,
    Cancelled,
    TimedOut,
    RetirementTimedOut,
    Worker,
    Closed,
    Exhausted,
}

#[derive(Clone, Copy)]
pub struct Policy {
    target: [u8; 64],
    target_len: u8,
    policy_sha256: [u8; 32],
    layout_sha256: [u8; 32],
    configuration_sha256: [u8; 32],
    key_sha256: [u8; 32],
    pub(crate) layout: Layout,
    pub(crate) minimum_counter: NonZeroU32,
}
impl Policy {
    /// The caller supplies independently trusted configuration and crypto root,
    /// never values selected by an incoming image or an untrusted download peer.
    /// This minimum is an admission floor, not a claim of protected device state.
    pub fn new(trust: &Trust<'_>, minimum_counter: NonZeroU32) -> Result<Self, Error> {
        trust.validate().map_err(|_| Error::Policy)?;
        let reserved =
            3 * u32::from(trust.layout.max_sectors) * u32::from(trust.layout.write_align) + 48;
        if reserved >= trust.layout.slot_bytes || trust.layout.erase_bytes > 4096 {
            return Err(Error::Policy);
        }
        let mut target = [0; 64];
        target[..trust.target.len()].copy_from_slice(trust.target.as_bytes());
        Ok(Self {
            target,
            target_len: trust.target.len() as u8,
            policy_sha256: trust.policy_sha256,
            layout_sha256: trust.layout_sha256,
            configuration_sha256: trust.configuration_sha256,
            key_sha256: trust.key_sha256,
            layout: trust.layout,
            minimum_counter,
        })
    }
    pub fn trust(&self) -> Trust<'_> {
        Trust {
            target: core::str::from_utf8(&self.target[..usize::from(self.target_len)])
                .expect("validated ASCII policy"),
            policy_sha256: self.policy_sha256,
            layout_sha256: self.layout_sha256,
            configuration_sha256: self.configuration_sha256,
            key_sha256: self.key_sha256,
            layout: self.layout,
        }
    }
    pub fn layout(&self) -> Layout {
        self.layout
    }
    pub fn admission_floor(&self) -> NonZeroU32 {
        self.minimum_counter
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StagedImage {
    pub security_counter: NonZeroU32,
    pub version: Version,
    pub payload_sha256: [u8; 32],
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RequestId(pub(crate) NonZeroU32);
impl RequestId {
    pub fn get(self) -> u32 {
        self.0.get()
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    Idle,
    Authenticating,
    Checking,
    Erasing,
    Writing,
    Verifying,
    Staged,
    Closing,
    Closed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Update {
    None,
    Completed {
        id: RequestId,
        result: Result<StagedImage, Error>,
        quiescent: bool,
    },
    Quiescent {
        id: RequestId,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Status {
    pub phase: Phase,
    pub completed_bytes: u32,
    pub fault: Option<Error>,
    pub retired: bool,
}
