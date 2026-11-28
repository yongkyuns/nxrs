//! M9 message admission and normalization over checked UBX frames.
use crate::{
    framing::{Event as WireEvent, Framer},
    DriverError, NavPvt,
};
use kinavis_ubx::{config, ConfigKey, Layers, Message, PollLayer};
use peripheral::gnss::{SatelliteReport, SatelliteStatus, TimePulse};

const WEEK_MS: u32 = 604_800_000;
const DEFAULT_SATELLITE_AGE_MS: u32 = 2_000;

pub(super) const TIM_TP_RATE: ConfigKey = match ConfigKey::new(0x2091_017e) {
    Ok(key) => key,
    Err(_) => panic!("TIM-TP UART1 key is U1"),
};

pub(super) enum Event<'a> {
    NavPvt(Result<&'a NavPvt, DriverError>),
    Satellites(SatelliteReport<'a>),
    Rate(ConfigKey, u8),
    Pulse(Option<TimePulse>),
    Navigation {
        measurement: u16,
        cycles: u16,
    },
    Ack(bool),
    Invalid,
}

#[derive(Default)]
pub(super) struct Decoder {
    parser: Framer,
    satellites: Option<(u32, SatelliteStatus)>,
    satellite_interval_ms: u32,
}

impl Decoder {
    pub(super) fn set_satellite_interval(&mut self, interval_ms: u32) {
        self.satellite_interval_ms = interval_ms;
    }

    pub(super) fn reset(&mut self) {
        self.parser = Framer::default();
        self.satellites = None;
    }

    // Share the decoder between acquisition and RAM transactions in the firmware.
    #[inline(never)]
    pub(super) fn consume(&mut self, bytes: &[u8], emit: &mut dyn FnMut(Event<'_>)) {
        for &byte in bytes {
            let frame = match self.parser.push(byte) {
                Some(WireEvent::Frame(frame)) => frame,
                Some(WireEvent::Satellites(epoch, mut report)) if epoch < WEEK_MS => {
                    self.satellites = Some((epoch, report.status));
                    report.interval_ms = self.satellite_interval_ms;
                    emit(Event::Satellites(report));
                    continue;
                }
                Some(WireEvent::Invalid | WireEvent::Satellites(..)) => {
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
                        self.satellites = None;
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
                (0x0d, 1) => {
                    emit(Event::Pulse(time_pulse(frame.payload)));
                    continue;
                }
                _ => continue,
            }
            match Message::decode(&frame) {
                Ok(Message::NavPvt(p)) => {
                    let speed = p.ground_speed.metres_per_second();
                    if speed < 0.0 || p.itow >= WEEK_MS {
                        emit(Event::NavPvt(Err(DriverError::InvalidData)));
                        continue;
                    }
                    let speed_accuracy = p.speed_accuracy.metres_per_second();
                    let heading_accuracy = p.heading_accuracy.degrees().to_radians();
                    let heading = (speed > speed_accuracy
                        && heading_accuracy < 180.0_f64.to_radians())
                    .then(|| p.heading_of_motion.degrees().to_radians() as f32);
                    let satellites = self.satellites.and_then(|(epoch, mut status)| {
                        // Ages repeat each receiver week. Keep matching unambiguous.
                        let limit = match self.satellite_interval_ms {
                            0 => DEFAULT_SATELLITE_AGE_MS,
                            interval => interval.min(WEEK_MS / 2 - 1),
                        };
                        let age = (p.itow + WEEK_MS - epoch) % WEEK_MS;
                        if age <= limit {
                            status.age_ms = age;
                            return Some(status);
                        }
                        if age < WEEK_MS - limit {
                            self.satellites = None;
                        }
                        None
                    });
                    emit(Event::NavPvt(Ok(&NavPvt {
                        itow_ms: p.itow,
                        // UBX-NAV-PVT valid.fullyResolved. instant() also checks
                        // the valid date/time flags and handles the signed fraction.
                        receiver_utc_ms: (frame.payload[11] & 4 != 0)
                            .then(|| p.instant().ok())
                            .flatten()
                            .and_then(|utc| utc.checked_unix_nanos())
                            .map(|nanos| nanos.div_euclid(1_000_000)),
                        latitude_deg: p.position.latitude().degrees(),
                        longitude_deg: p.position.longitude().degrees(),
                        speed_mps: speed as f32,
                        speed_accuracy_mps: speed_accuracy as f32,
                        horizontal_accuracy_m: p.horizontal_accuracy.metres() as f32,
                        vertical_accuracy_m: p.vertical_accuracy.metres() as f32,
                        position_dop: p.pdop.map(|dop| dop.value() as f32),
                        satellites_used: p.satellites,
                        satellites,
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
                    match (
                        p.layer,
                        p.position,
                        values.next(),
                        values.next(),
                        values.next(),
                    ) {
                        (0, 0, Some(Ok((key, rate))), None, None)
                            if matches!(
                                key,
                                ConfigKey::MSGOUT_NAV_PVT_UART1
                                    | ConfigKey::MSGOUT_NAV_SAT_UART1
                                    | TIM_TP_RATE
                            ) && rate <= u8::MAX.into() =>
                        {
                            emit(Event::Rate(key, rate as u8))
                        }
                        (
                            0,
                            0,
                            Some(Ok((ConfigKey::RATE_MEAS, measurement))),
                            Some(Ok((ConfigKey::RATE_NAV, cycles))),
                            None,
                        ) if measurement <= u16::MAX.into() && cycles <= u16::MAX.into() => {
                            emit(Event::Navigation {
                                measurement: measurement as u16,
                                cycles: cycles as u16,
                            })
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

fn time_pulse(raw: &[u8]) -> Option<TimePulse> {
    // TpNotLocked describes the edge. qErrInvalid only describes the optional
    // picosecond correction, which is below the device clock's resolution.
    if raw.len() != 16 || raw[14] & 0x20 != 0 || raw[4..8] != [0; 4] {
        return None;
    }
    let tow = u32::from_le_bytes(raw[..4].try_into().unwrap());
    if tow >= WEEK_MS || tow % 1_000 != 0 {
        return None;
    }
    match raw[14] & 3 {
        0 | 2 if raw[15] & 0x0f == 0 => Some(TimePulse::Gps(tow)),
        3 => Some(TimePulse::Utc(tow)),
        _ => None,
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
pub(super) fn get_rate(key: ConfigKey) -> Command {
    let mut out = Command::default();
    out.used = config::valget(
        PollLayer::Ram,
        0,
        &[key],
        &mut out.bytes,
    )
    .expect("one U1 key fits the fixed command buffer");
    out
}
pub(super) fn set_rate(key: ConfigKey, rate: u8) -> Command {
    let mut out = Command::default();
    out.used = config::valset(
        Layers::RAM,
        &[(key, rate.into())],
        &mut out.bytes,
    )
    .expect("one U1 value fits the fixed command buffer");
    out
}

pub(super) fn get_navigation() -> Command {
    let mut out = Command::default();
    out.used = config::valget(
        PollLayer::Ram,
        0,
        &[ConfigKey::RATE_MEAS, ConfigKey::RATE_NAV],
        &mut out.bytes,
    )
    .expect("two keys fit the fixed command buffer");
    out
}

pub(super) fn set_navigation(measurement: u16, cycles: u16) -> Command {
    let mut out = Command::default();
    out.used = config::valset(
        Layers::RAM,
        &[
            (ConfigKey::RATE_MEAS, measurement.into()),
            (ConfigKey::RATE_NAV, cycles.into()),
        ],
        &mut out.bytes,
    )
    .expect("two U2 values fit the fixed command buffer");
    out
}
