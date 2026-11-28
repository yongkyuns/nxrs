//! M9 message admission and normalization over checked UBX frames.
use crate::{
    framing::{Event as WireEvent, Framer},
    DriverError, NavPvt,
};
use kinavis_ubx::{config, ConfigKey, Layers, Message, PollLayer};
use peripheral::gnss::{SatelliteReport, SatelliteStatus, TimePulse};
use std::time::Duration;

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
    bytes: [u8; 32],
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

// UBX-21022436 R02, sections 3.13.9.2 and 3.13.9.3. Keep the initial
// position/time layouts here; they are not navigation output messages.
pub(super) fn initial_position(
    latitude_deg: f64,
    longitude_deg: f64,
    ellipsoid_altitude_m: f64,
    accuracy_m: f64,
) -> Result<Command, DriverError> {
    if !(-90.0..=90.0).contains(&latitude_deg)
        || !(-180.0..=180.0).contains(&longitude_deg)
        || !ellipsoid_altitude_m.is_finite()
        || !accuracy_m.is_finite()
        || accuracy_m <= 0.0
    {
        return Err(DriverError::InvalidData);
    }
    let altitude_cm = (ellipsoid_altitude_m * 100.0).round();
    // A centimetre also covers combined rounding of latitude, longitude and
    // ellipsoid altitude. Never advertise more precision after wire encoding.
    let accuracy_cm = (accuracy_m * 100.0).ceil() + 1.0;
    if !(i32::MIN as f64..=i32::MAX as f64).contains(&altitude_cm) || accuracy_cm > u32::MAX as f64
    {
        return Err(DriverError::InvalidData);
    }
    let mut payload = [0; 20];
    payload[0] = 1; // POS_LLH; version and reserved bytes remain zero.
    payload[4..8].copy_from_slice(&((latitude_deg * 1e7).round() as i32).to_le_bytes());
    payload[8..12].copy_from_slice(&((longitude_deg * 1e7).round() as i32).to_le_bytes());
    payload[12..16].copy_from_slice(&(altitude_cm as i32).to_le_bytes());
    payload[16..20].copy_from_slice(&(accuracy_cm as u32).to_le_bytes());
    Ok(initial_message(&payload))
}

pub(super) fn initial_time(
    since_unix_epoch: Duration,
    accuracy: Duration,
) -> Result<Command, DriverError> {
    let seconds = since_unix_epoch.as_secs();
    let (year, month, day) = utc_date(seconds / 86_400)?;
    let second_of_day = (seconds % 86_400) as u32;
    let accuracy_seconds =
        u16::try_from(accuracy.as_secs()).map_err(|_| DriverError::InvalidData)?;
    let mut payload = [0; 24];
    payload[0] = 0x10; // TIME_UTC, version 0, reference = message receipt.
    payload[3] = 0x80; // The UTC input does not establish a leap-second count.
    payload[4..6].copy_from_slice(&year.to_le_bytes());
    payload[6] = month;
    payload[7] = day;
    payload[8] = (second_of_day / 3_600) as u8;
    payload[9] = ((second_of_day / 60) % 60) as u8;
    payload[10] = (second_of_day % 60) as u8;
    // trustedSource stays clear: accuracy is not independent spoofing trust.
    payload[12..16].copy_from_slice(&since_unix_epoch.subsec_nanos().to_le_bytes());
    payload[16..18].copy_from_slice(&accuracy_seconds.to_le_bytes());
    payload[20..24].copy_from_slice(&accuracy.subsec_nanos().to_le_bytes());
    Ok(initial_message(&payload))
}

fn initial_message(payload: &[u8]) -> Command {
    let mut out = Command::default();
    let end = 6 + payload.len();
    out.bytes[..6].copy_from_slice(&[0xb5, 0x62, 0x13, 0x40, payload.len() as u8, 0]);
    out.bytes[6..end].copy_from_slice(payload);
    let (a, b) = kinavis_ubx::checksum(0x13, 0x40, payload);
    out.bytes[end..end + 2].copy_from_slice(&[a, b]);
    out.used = end + 2;
    out
}

fn utc_date(unix_days: u64) -> Result<(u16, u8, u8), DriverError> {
    // Gregorian 400-year eras, with March as month zero, avoid a calendar
    // dependency and handle the century exceptions (including year 2100).
    // Every representable U2 year needs fewer than 24 million Unix days, so
    // checked 32-bit calendar arithmetic covers the complete wire range.
    let days = i32::try_from(unix_days)
        .ok()
        .and_then(|days| days.checked_add(719_468))
        .ok_or(DriverError::InvalidData)?;
    let era = days / 146_097;
    let day_of_era = days % 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let march_month = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * march_month + 2) / 5 + 1;
    let month = march_month + if march_month < 10 { 3 } else { -9 };
    let year = year_of_era + era * 400 + i32::from(month <= 2);
    Ok((
        u16::try_from(year).map_err(|_| DriverError::InvalidData)?,
        month as u8,
        day as u8,
    ))
}
