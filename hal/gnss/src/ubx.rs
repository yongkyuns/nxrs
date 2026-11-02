//! M9 message admission/normalization; UBX framing and codecs are independent.
use crate::{DeviceError, GnssFix};
use kinavis_ubx::{config, ConfigKey, Deframer, Event as WireEvent, Layers, Message, PollLayer};

pub(super) enum Event {
    Fix(Result<GnssFix, DeviceError>),
    Rate(u8),
    Ack(bool),
    Invalid,
}
pub(super) struct Decoder {
    parser: Deframer<92>,
    sequence: u64,
    last_epoch: Option<u32>,
}
impl Default for Decoder {
    fn default() -> Self {
        Self {
            parser: Deframer::new(),
            sequence: 0,
            last_epoch: None,
        }
    }
}
impl Decoder {
    pub(super) fn consume(&mut self, bytes: &[u8], now_ms: u64, mut emit: impl FnMut(Event)) {
        for &byte in bytes {
            let frame = match self.parser.push(byte) {
                Some(WireEvent::Frame(frame)) => frame,
                Some(WireEvent::Error(_)) => {
                    emit(Event::Invalid);
                    continue;
                }
                _ => continue,
            };
            match (frame.class, frame.id) {
                (1, 7) => {
                    // The codec supports an older 84B NAV-PVT too and wraps
                    // longitudes/courses. M9 requires 92B and in-domain values;
                    // reject malformed wire values before that normalization.
                    let raw = frame.payload;
                    if raw.len() != 92 {
                        emit(Event::Invalid);
                        continue;
                    }
                    // Unavailable solution fields are unspecified. Classify
                    // validity before the codec interprets any coordinates.
                    if raw[21] & 1 == 0 || raw[78] & 1 != 0 || !matches!(raw[20], 2 | 3) {
                        emit(Event::Fix(Err(DeviceError::Busy)));
                        continue;
                    }
                    let lon = i32::from_le_bytes(raw[24..28].try_into().unwrap());
                    let course = i32::from_le_bytes(raw[64..68].try_into().unwrap());
                    if !(-1_800_000_000..=1_800_000_000).contains(&lon)
                        || !(-18_000_000..=36_000_000).contains(&course)
                    {
                        emit(Event::Invalid);
                        continue;
                    }
                }
                (5, 0 | 1) | (6, 0x8b) => {}
                _ => continue,
            }
            match Message::decode(&frame) {
                Ok(Message::NavPvt(p)) => {
                    let speed = p.ground_speed.metres_per_second();
                    if speed < 0.0 {
                        emit(Event::Fix(Err(DeviceError::InvalidData)));
                        continue;
                    }
                    if self.last_epoch == Some(p.itow) {
                        continue;
                    }
                    self.last_epoch = Some(p.itow);
                    self.sequence = self.sequence.saturating_add(1);
                    emit(Event::Fix(Ok(GnssFix {
                        sequence: self.sequence,
                        timestamp_ms: now_ms,
                        latitude_deg: p.position.latitude().degrees(),
                        longitude_deg: p.position.longitude().degrees(),
                        speed_mps: speed as f32,
                        course_rad: (speed > p.speed_accuracy.metres_per_second()
                            && p.heading_accuracy.degrees() < 180.0)
                            .then(|| p.heading_of_motion.degrees().to_radians() as f32),
                    })));
                }
                Ok(Message::Ack { class: 6, id: 0x8a }) => emit(Event::Ack(true)),
                Ok(Message::Nak {
                    class: 6,
                    id: 0x8a | 0x8b,
                }) => emit(Event::Ack(false)),
                Ok(Message::CfgValget(p)) => {
                    let mut values = p.values();
                    match values.next() {
                        Some(Ok((ConfigKey::MSGOUT_NAV_PVT_UART1, rate)))
                            if p.layer == 0
                                && p.position == 0
                                && rate <= u8::MAX.into()
                                && values.next().is_none() =>
                        {
                            emit(Event::Rate(rate as u8))
                        }
                        _ => emit(Event::Invalid),
                    }
                }
                Err(_) => emit(Event::Invalid),
                _ => {}
            }
        }
    }
}
#[derive(Default)]
pub(super) struct Command {
    bytes: [u8; 24],
    used: usize,
}
impl core::ops::Deref for Command {
    type Target = [u8];
    fn deref(&self) -> &[u8] {
        &self.bytes[..self.used]
    }
}
pub(super) fn get_rate() -> Command {
    let mut out = Command::default();
    out.used = config::valget(
        PollLayer::Ram,
        0,
        &[ConfigKey::MSGOUT_NAV_PVT_UART1],
        &mut out.bytes,
    )
    .expect("one U1 key fits the fixed command buffer");
    out
}
pub(super) fn set_rate(rate: u8) -> Command {
    let mut out = Command::default();
    out.used = config::valset(
        Layers::RAM,
        &[(ConfigKey::MSGOUT_NAV_PVT_UART1, rate.into())],
        &mut out.bytes,
    )
    .expect("one U1 value fits the fixed command buffer");
    out
}
