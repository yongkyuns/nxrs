//! M9 message admission and normalization; wire framing/codecs use kinavis-ubx.
use crate::{DriverError, NavPvt};
use kinavis_ubx::{config, ConfigKey, Deframer, Event as WireEvent, Layers, Message, PollLayer};

pub(super) enum Event {
    NavPvt(Result<NavPvt, DriverError>),
    Rate(u8),
    Ack(bool),
    Invalid,
}

pub(super) struct Decoder {
    parser: Deframer<92>,
}

impl Default for Decoder {
    fn default() -> Self {
        Self {
            parser: Deframer::new(),
        }
    }
}

impl Decoder {
    pub(super) fn consume(&mut self, bytes: &[u8], mut emit: impl FnMut(Event)) {
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
                    let raw = frame.payload;
                    if raw.len() != 92 {
                        emit(Event::Invalid);
                        continue;
                    }
                    if raw[21] & 1 == 0 || raw[78] & 1 != 0 || !matches!(raw[20], 2 | 3) {
                        emit(Event::NavPvt(Err(DriverError::NoFix)));
                        continue;
                    }
                    let lon = i32::from_le_bytes(raw[24..28].try_into().unwrap());
                    let lat = i32::from_le_bytes(raw[28..32].try_into().unwrap());
                    let course = i32::from_le_bytes(raw[64..68].try_into().unwrap());
                    if !(-1_800_000_000..=1_800_000_000).contains(&lon)
                        || !(-900_000_000..=900_000_000).contains(&lat)
                        || !(-18_000_000..=36_000_000).contains(&course)
                    {
                        emit(Event::NavPvt(Err(DriverError::InvalidData)));
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
                        emit(Event::NavPvt(Err(DriverError::InvalidData)));
                        continue;
                    }
                    let speed_accuracy = p.speed_accuracy.metres_per_second();
                    let heading_accuracy = p.heading_accuracy.degrees().to_radians();
                    let heading = (speed > speed_accuracy
                        && heading_accuracy < 180.0_f64.to_radians())
                    .then(|| p.heading_of_motion.degrees().to_radians() as f32);
                    emit(Event::NavPvt(Ok(NavPvt {
                        itow_ms: p.itow,
                        latitude_deg: p.position.latitude().degrees(),
                        longitude_deg: p.position.longitude().degrees(),
                        speed_mps: speed as f32,
                        speed_accuracy_mps: speed_accuracy as f32,
                        heading_rad: heading,
                        heading_accuracy_rad: heading_accuracy as f32,
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
