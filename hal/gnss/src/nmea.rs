//! Bounded RMC decoder, shared by receiver providers. No I/O or scheduling.
#![forbid(unsafe_code)]
use crate::{DeviceError, GnssFix};

/// One sentence buffer; unrelated sentences are ignored. A fix is emitted only
/// from a newly received, checksum-valid active RMC sentence.
pub struct Decoder {
    line: [u8; 128],
    used: usize,
    receiving: bool,
    sequence: u64,
}

impl Default for Decoder {
    fn default() -> Self {
        Self {
            line: [0; 128],
            used: 0,
            receiving: false,
            sequence: 0,
        }
    }
}

impl Decoder {
    /// `now_ms` is the owner's monotonic reception time, not a UTC claim.
    pub fn push(&mut self, byte: u8, now_ms: u64) -> Option<Result<GnssFix, DeviceError>> {
        if byte == b'$' {
            self.used = 0;
            self.receiving = true;
            return None;
        }
        if !self.receiving || byte == b'\r' {
            return None;
        }
        if byte == b'\n' {
            self.receiving = false;
            let parsed = parse(&self.line[..self.used]);
            return match parsed {
                Ok(Some((latitude_deg, longitude_deg, speed_mps, course_rad))) => {
                    self.sequence = self.sequence.saturating_add(1);
                    Some(Ok(GnssFix {
                        sequence: self.sequence,
                        timestamp_ms: now_ms,
                        latitude_deg,
                        longitude_deg,
                        speed_mps,
                        course_rad,
                    }))
                }
                Ok(None) => None,
                Err(error) => Some(Err(error)),
            };
        }
        if self.used == self.line.len() {
            self.receiving = false;
            return Some(Err(DeviceError::Full));
        }
        self.line[self.used] = byte;
        self.used += 1;
        None
    }
}

type Position = (f64, f64, f32, Option<f32>);
fn parse(line: &[u8]) -> Result<Option<Position>, DeviceError> {
    let invalid = DeviceError::InvalidData;
    let text = core::str::from_utf8(line).map_err(|_| invalid)?;
    let (body, checksum) = text.rsplit_once('*').ok_or(invalid)?;
    if checksum.len() != 2
        || u8::from_str_radix(checksum, 16).map_err(|_| invalid)?
            != body.bytes().fold(0, |a, b| a ^ b)
    {
        return Err(invalid);
    }
    let mut fields = body.split(',');
    let id = fields.next().ok_or(invalid)?;
    if id.len() != 5 || &id.as_bytes()[2..] != b"RMC" {
        return Ok(None);
    }
    let time = fields.next().ok_or(invalid)?;
    let (whole, fraction) = time
        .split_once('.')
        .map_or((time, None), |(w, f)| (w, Some(f)));
    if whole.len() != 6
        || !whole.bytes().all(|b| b.is_ascii_digit())
        || fraction.is_some_and(|f| f.is_empty() || !f.bytes().all(|b| b.is_ascii_digit()))
        || whole[..2].parse::<u8>().map_err(|_| invalid)? > 23
        || whole[2..4].parse::<u8>().map_err(|_| invalid)? > 59
        || whole[4..].parse::<u8>().map_err(|_| invalid)? > 60
    {
        return Err(invalid);
    }
    match fields.next() {
        Some("A") => {}
        Some("V") => return Err(DeviceError::Busy),
        _ => return Err(invalid),
    }
    let latitude = coordinate(fields.next(), fields.next(), true)?;
    let longitude = coordinate(fields.next(), fields.next(), false)?;
    let speed: f32 = fields.next().ok_or(invalid)?.parse().map_err(|_| invalid)?;
    let heading = fields.next().ok_or(invalid)?;
    let course: Option<f32> = if heading.is_empty() {
        None
    } else {
        Some(heading.parse().map_err(|_| invalid)?)
    };
    if !speed.is_finite()
        || speed < 0.0
        || course.is_some_and(|c| !c.is_finite() || !(0.0..=360.0).contains(&c))
    {
        return Err(invalid);
    }
    Ok(Some((
        latitude,
        longitude,
        speed * (1852.0 / 3600.0),
        course.map(f32::to_radians),
    )))
}

fn coordinate(
    value: Option<&str>,
    direction: Option<&str>,
    latitude: bool,
) -> Result<f64, DeviceError> {
    let invalid = DeviceError::InvalidData;
    let value = value.ok_or(invalid)?;
    let digits = if latitude { 2 } else { 3 };
    if value.len() < digits + 2 || !value.bytes().all(|b| b.is_ascii_digit() || b == b'.') {
        return Err(invalid);
    }
    let (degrees, minutes) = value.split_at(digits);
    if minutes.split('.').next().ok_or(invalid)?.len() != 2 {
        return Err(invalid);
    }
    let degrees: f64 = degrees.parse().map_err(|_| invalid)?;
    let minutes: f64 = minutes.parse().map_err(|_| invalid)?;
    let limit = if latitude { 90.0 } else { 180.0 };
    if !minutes.is_finite()
        || !(0.0..60.0).contains(&minutes)
        || degrees > limit
        || (degrees == limit && minutes != 0.0)
    {
        return Err(invalid);
    }
    let sign = match (latitude, direction) {
        (true, Some("N")) | (false, Some("E")) => 1.0,
        (true, Some("S")) | (false, Some("W")) => -1.0,
        _ => return Err(invalid),
    };
    Ok(sign * (degrees + minutes / 60.0))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn sentence(decoder: &mut Decoder, body: &str) -> Option<Result<GnssFix, DeviceError>> {
        let checksum = body.bytes().fold(0, |a, b| a ^ b);
        let wire = std::format!("${body}*{checksum:02X}\r\n");
        wire.bytes().filter_map(|b| decoder.push(b, 123)).last()
    }
    #[test]
    fn fragmented_fix_converts_coordinates_units_and_hemispheres() {
        let mut d = Decoder::default();
        let fix = sentence(
            &mut d,
            "GNRMC,120000.00,A,4807.038,N,01131.000,E,10.0,90.0,010126,,,A",
        )
        .unwrap()
        .unwrap();
        assert!((fix.latitude_deg - 48.1173).abs() < 1e-6);
        assert!((fix.longitude_deg - 11.5166667).abs() < 1e-6);
        assert!((fix.speed_mps - 5.144444).abs() < 1e-5);
        assert!((fix.course_rad.unwrap() - core::f32::consts::FRAC_PI_2).abs() < 1e-6);
        assert_eq!((fix.sequence, fix.timestamp_ms), (1, 123));
        let fix = sentence(
            &mut d,
            "GPRMC,120001.00,A,4807.038,S,01131.000,W,0,,010126,,,A",
        )
        .unwrap()
        .unwrap();
        assert!(fix.latitude_deg < 0.0 && fix.longitude_deg < 0.0);
        assert_eq!(fix.sequence, 2);
        assert_eq!(fix.course_rad, None);
    }
    #[test]
    fn rejects_invalid_or_unavailable_fixes_without_advancing_sequence() {
        let mut d = Decoder::default();
        for (body, expected) in [
            ("GNRMC,120000,V,,,,,,,010126,,,N", DeviceError::Busy),
            (
                "GNRMC,120000.1.2,A,4807.0,N,01131.0,E,1,0",
                DeviceError::InvalidData,
            ),
            (
                "GNRMC,120000,A,4860.0,N,01131.0,E,1,0",
                DeviceError::InvalidData,
            ),
            (
                "GNRMC,120000,A,4807.0,N,18100.0,E,1,0",
                DeviceError::InvalidData,
            ),
            (
                "GNRMC,120000,A,4807.0,N,01131.0,E,NaN,0",
                DeviceError::InvalidData,
            ),
        ] {
            assert_eq!(sentence(&mut d, body), Some(Err(expected)));
        }
        assert_eq!(
            b"$GNRMC,broken*00\n"
                .iter()
                .filter_map(|&b| d.push(b, 0))
                .last(),
            Some(Err(DeviceError::InvalidData))
        );
        assert_eq!(d.sequence, 0);
    }
    #[test]
    fn bounds_input_resynchronizes_and_ignores_other_sentences() {
        let mut d = Decoder::default();
        d.push(b'$', 0);
        for _ in 0..128 {
            assert_eq!(d.push(b'x', 0), None);
        }
        assert_eq!(d.push(b'x', 0), Some(Err(DeviceError::Full)));
        assert!(sentence(&mut d, "GNGGA,ignored").is_none());
        assert!(sentence(&mut d, "GNRMC,120000,A,0000.0,N,00000.0,E,0,0")
            .unwrap()
            .is_ok());
    }
}
