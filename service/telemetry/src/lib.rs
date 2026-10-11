//! Compact frame telemetry service over a packet sink capability.
#![no_std]
#![forbid(unsafe_code)]

use camera::{Frame, PixelFormat};
use transport::{DeviceError, PacketSink};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Device(DeviceError),
}

impl From<DeviceError> for Error {
    fn from(value: DeviceError) -> Self { Self::Device(value) }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct TelemetryStats { pub accepted: u64, pub errors: u64 }

pub trait Telemetry {
    fn publish(&mut self, frame: Frame<'_>) -> Result<(), Error>;
}

/// Version 1, 28-byte little-endian summary, independent of native ABI:
/// version:u8, format:u8, width:u16, height:u16, reserved:u16,
/// sequence:u64, timestamp_ms:u64, payload_fnv1a:u32.
/// This diagnostic checksum is not an integrity/security protocol.
pub const SUMMARY_BYTES: usize = 28;

pub fn payload_checksum(bytes: &[u8]) -> u32 {
    bytes.iter().fold(2_166_136_261, |hash, byte| {
        (hash ^ u32::from(*byte)).wrapping_mul(16_777_619)
    })
}

pub struct TelemetryService<D> { device: D, stats: TelemetryStats }

impl<D: PacketSink> TelemetryService<D> {
    pub fn new(device: D) -> Self { Self { device, stats: TelemetryStats::default() } }
    pub fn stats(&self) -> TelemetryStats { self.stats }
    pub fn backend(&self) -> &D { &self.device }
}

impl<D: PacketSink> Telemetry for TelemetryService<D> {
    fn publish(&mut self, frame: Frame<'_>) -> Result<(), Error> {
        let mut packet = [0u8; SUMMARY_BYTES];
        packet[0] = 1;
        packet[1] = match frame.capture.format.pixels {
            PixelFormat::Gray8 => 0,
            PixelFormat::Rgb565 => 1,
            PixelFormat::Jpeg => 2,
        };
        packet[2..4].copy_from_slice(&frame.capture.format.width.to_le_bytes());
        packet[4..6].copy_from_slice(&frame.capture.format.height.to_le_bytes());
        packet[8..16].copy_from_slice(&frame.sequence.to_le_bytes());
        packet[16..24].copy_from_slice(&frame.capture.timestamp_ms.to_le_bytes());
        packet[24..28].copy_from_slice(&payload_checksum(frame.bytes).to_le_bytes());
        match self.device.send(&packet) {
            Ok(()) => {
                self.stats.accepted = self.stats.accepted.saturating_add(1);
                Ok(())
            }
            Err(error) => {
                self.stats.errors = self.stats.errors.saturating_add(1);
                Err(error.into())
            }
        }
    }
}
