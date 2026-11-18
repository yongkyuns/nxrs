//! One authenticated RTSP session for H.264/Opus. Both RTP clocks are mapped
//! through RTCP sender reports before an A/V viewer can subscribe.
use super::*;
use bytes::BytesMut;
use retina::client::{PacketItem, Playing};
use retina::codec::VideoParametersCodec;
use std::time::Instant as ClockTime;

// MediaMTX normally sends reports about ten seconds apart.
const REPORT_WAIT: Duration = Duration::from_secs(25);

#[derive(Default)]
struct Clock {
    ssrc: Option<u32>,
    report: Option<(u32, ClockTime, ClockTime)>,
}

impl Clock {
    fn source(&mut self, ssrc: u32) -> Result<(), ()> {
        (*self.ssrc.get_or_insert(ssrc) == ssrc)
            .then_some(())
            .ok_or(())
    }

    fn map(&self, tick: u32, hz: u32) -> Result<Option<ClockTime>, ()> {
        let Some((anchor, time, received)) = self.report else {
            return Ok(None);
        };
        if received.elapsed() > REPORT_WAIT {
            return Err(());
        }
        let delta = tick.wrapping_sub(anchor) as i32;
        let duration =
            Duration::from_nanos(u64::from(delta.unsigned_abs()) * 1_000_000_000 / u64::from(hz));
        if duration > REPORT_WAIT {
            return Err(());
        }
        if delta < 0 {
            time.checked_sub(duration)
        } else {
            time.checked_add(duration)
        }
        .map(Some)
        .ok_or(())
    }
}

fn report_time(origin: (u64, ClockTime), ntp: u64) -> Result<ClockTime, ()> {
    let delta = ntp.wrapping_sub(origin.0) as i64;
    let ticks = delta.unsigned_abs();
    let offset = Duration::new(
        ticks >> 32,
        (((ticks & 0xffff_ffff) * 1_000_000_000) >> 32) as u32,
    );
    if delta < 0 {
        origin.1.checked_sub(offset)
    } else {
        origin.1.checked_add(offset)
    }
    .ok_or(())
}

pub(super) async fn receive(
    reader: &Reader,
    generation: u64,
    mut media: Session<Playing>,
    video: usize,
    audio: usize,
) -> Result<(), ()> {
    let Some(ParametersRef::Video(parameters)) = media.streams()[video].parameters() else {
        return Err(());
    };
    let dimensions = parameters.pixel_dimensions();
    let VideoParametersCodec::H264 { sps, pps } = parameters.codec_params() else {
        return Err(());
    };
    if !(2..=1920).contains(&dimensions.0)
        || !(2..=1080).contains(&dimensions.1)
        || sps.len() + pps.len() + 8 >= MAX_UNIT
    {
        return Err(());
    }
    let mut picture = H264::new(sps.clone(), pps.clone());
    let mut clocks = [Clock::default(), Clock::default()];
    let mut origin = None;
    let mut deadlines = [Instant::now() + SILENCE; 2];
    let startup = Instant::now() + REPORT_WAIT;
    let mut first = [None, None];
    let mut previous = [None, None];
    let mut joining = true;
    loop {
        let mut deadline = deadlines[0].min(deadlines[1]);
        if first.contains(&None) {
            deadline = deadline.min(startup);
        }
        if Instant::now() >= deadline {
            return Err(());
        }
        let item = time::timeout_at(deadline, media.next())
            .await
            .map_err(|_| ())?
            .ok_or(())?
            .map_err(|_| ())?;
        let stream = match &item {
            PacketItem::Rtp(p) => p.stream_id(),
            PacketItem::Rtcp(p) => p.stream_id(),
            _ => return Err(()),
        };
        let track = if stream == video {
            0
        } else if stream == audio {
            1
        } else {
            return Err(());
        };
        let rate = [90_000, 48_000][track];
        match item {
            PacketItem::Rtcp(packet) => {
                let Some(report) = packet
                    .pkts()
                    .next()
                    .and_then(|p| p.as_sender_report().ok())
                    .flatten()
                else {
                    continue;
                };
                clocks[track].source(report.ssrc())?;
                let now = ClockTime::now();
                let ntp = report.ntp_timestamp().0;
                let mapped = report_time(*origin.get_or_insert((ntp, now)), ntp)?;
                if mapped.max(now) - mapped.min(now) > REPORT_WAIT {
                    return Err(());
                }
                if let Some(expected) = clocks[track].map(report.rtp_timestamp(), rate)? {
                    if expected.max(mapped) - expected.min(mapped) > Duration::from_millis(100) {
                        return Err(());
                    }
                }
                clocks[track].report = Some((report.rtp_timestamp(), mapped, now));
            }
            PacketItem::Rtp(packet) => {
                if packet.loss() != 0 || packet.payload().is_empty() {
                    return Err(());
                }
                clocks[track].source(packet.ssrc())?;
                let Some(source_time) = clocks[track].map(packet.header().timestamp(), rate)?
                else {
                    deadlines[track] = Instant::now() + SILENCE;
                    continue;
                };
                let tick = packet.timestamp().timestamp();
                let (keyframe, data) = if track == 0 {
                    if joining {
                        joining = !packet.mark();
                        continue;
                    }
                    let Some(unit) = picture.push(tick, packet.payload(), packet.mark())? else {
                        continue;
                    };
                    if first[0].is_none() && !unit.0 {
                        continue;
                    }
                    unit
                } else {
                    if packet.payload().len() > 4096 {
                        return Err(());
                    }
                    (false, packet.into_payload_bytes())
                };
                if previous[track].is_some_and(|p| tick <= p) {
                    return Err(());
                }
                previous[track] = Some(tick);
                let pts = u64::try_from(
                    tick.checked_sub(*first[track].get_or_insert(tick))
                        .ok_or(())?,
                )
                .map_err(|_| ())?;
                deadlines[track] = Instant::now() + SILENCE;
                if track == 0 {
                    let unit = AccessUnit {
                        pts_90khz: pts,
                        source_time,
                        keyframe,
                        dimensions,
                        annex_b: data,
                    };
                    reader.publish_picture(generation, unit);
                } else {
                    let _ = reader.sound.send(Some((
                        generation,
                        AudioPacket {
                            pts_48khz: pts,
                            source_time,
                            opus: data,
                        },
                    )));
                }
                if !first.contains(&None) {
                    reader.opus_generation.store(generation, Ordering::Release);
                }
            }
            _ => return Err(()),
        }
    }
}

// RFC 6184 non-interleaved mode. Assemble one bounded picture and require
// the declared SPS/PPS to remain stable until the source is reopened.
struct H264 {
    sps: Bytes,
    pps: Bytes,
    data: BytesMut,
    tick: Option<i64>,
    fragment: Option<(usize, u8)>,
    keyframe: bool,
    has_slice: bool,
}

impl H264 {
    fn new(sps: Bytes, pps: Bytes) -> Self {
        Self {
            sps,
            pps,
            data: BytesMut::new(),
            tick: None,
            fragment: None,
            keyframe: false,
            has_slice: false,
        }
    }

    fn append(&mut self, data: &[u8]) -> Result<(), ()> {
        if self.data.len() + data.len() + self.sps.len() + self.pps.len() + 8 > MAX_UNIT {
            return Err(());
        }
        self.data.extend_from_slice(data);
        Ok(())
    }

    fn complete_nal(&mut self, start: usize) -> Result<(), ()> {
        let nal = &self.data[start..];
        let kind = nal[0] & 31;
        self.has_slice |= (1..=5).contains(&kind);
        match kind {
            5 => self.keyframe = true,
            7 if nal != self.sps.as_ref() => return Err(()),
            8 if nal != self.pps.as_ref() => return Err(()),
            1..=23 => {}
            _ => return Err(()),
        }
        Ok(())
    }

    fn nal(&mut self, nal: &[u8]) -> Result<(), ()> {
        if self.fragment.is_some() || nal.is_empty() || nal[0] & 0x80 != 0 {
            return Err(());
        }
        self.append(&[0, 0, 0, 1])?;
        let start = self.data.len();
        self.append(nal)?;
        self.complete_nal(start)
    }

    fn push(&mut self, tick: i64, payload: &[u8], mark: bool) -> Result<Option<(bool, Bytes)>, ()> {
        if *self.tick.get_or_insert(tick) != tick {
            return Err(());
        }
        let (&indicator, mut rest) = payload.split_first().ok_or(())?;
        if indicator & 0x80 != 0 {
            return Err(());
        }
        match indicator & 31 {
            1..=23 => self.nal(payload)?,
            24 if !rest.is_empty() => {
                while !rest.is_empty() {
                    let (size, body) = rest.split_at_checked(2).ok_or(())?;
                    let (nal, next) = body
                        .split_at_checked(u16::from_be_bytes([size[0], size[1]]) as usize)
                        .ok_or(())?;
                    self.nal(nal)?;
                    rest = next;
                }
            }
            28 => {
                let (&flags, chunk) = rest.split_first().ok_or(())?;
                if chunk.is_empty() || flags & 0x20 != 0 || !(1..=23).contains(&(flags & 31)) {
                    return Err(());
                }
                let header = (indicator & 0x60) | (flags & 31);
                if flags & 0x80 != 0 {
                    if self.fragment.is_some() || flags & 0x40 != 0 {
                        return Err(());
                    }
                    self.append(&[0, 0, 0, 1])?;
                    self.fragment = Some((self.data.len(), header));
                    self.append(&[header])?;
                }
                let (start, old) = self.fragment.ok_or(())?;
                if old != header {
                    return Err(());
                }
                self.append(chunk)?;
                if flags & 0x40 != 0 {
                    self.complete_nal(start)?;
                    self.fragment = None;
                }
            }
            _ => return Err(()),
        }
        if !mark {
            return Ok(None);
        }
        if self.fragment.is_some() {
            return Err(());
        }
        self.tick = None;
        let key = std::mem::take(&mut self.keyframe);
        let data = self.data.split().freeze();
        if !std::mem::take(&mut self.has_slice) {
            return Ok(None);
        }
        if !key {
            return Ok(Some((false, data)));
        }
        let mut out = BytesMut::with_capacity(8 + self.sps.len() + self.pps.len() + data.len());
        for part in [&self.sps, &self.pps] {
            out.extend_from_slice(&[0, 0, 0, 1]);
            out.extend_from_slice(part);
        }
        out.extend_from_slice(&data);
        Ok(Some((true, out.freeze())))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parser() -> H264 {
        H264::new(
            Bytes::from_static(&[0x67, 1]),
            Bytes::from_static(&[0x68, 2]),
        )
    }

    #[test]
    fn fragmented_idr_uses_declared_parameters_and_joinable_start_codes() {
        let mut h264 = parser();
        assert!(h264
            .push(3, &[0x78, 0, 2, 0x67, 1, 0, 2, 0x68, 2], false)
            .unwrap()
            .is_none());
        assert!(h264.push(3, &[0x7c, 0x85, 2], false).unwrap().is_none());
        let (key, picture) = h264.push(3, &[0x7c, 0x45, 3], true).unwrap().unwrap();
        assert!(key);
        assert!(picture.starts_with(&[0, 0, 0, 1, 0x67, 1, 0, 0, 0, 1, 0x68, 2]));
        assert!(picture.ends_with(&[0, 0, 0, 1, 0x65, 2, 3]));
    }

    #[test]
    fn malformed_fragment_changed_parameters_and_oversized_picture_are_rejected() {
        for nal in [
            &[0x78, 0][..],
            &[0x78, 0, 0],
            &[0x67, 2],
            &[0x7c, 0x45, 2],
            &[0x7c, 0x85, 2],
        ] {
            assert!(parser().push(0, nal, true).is_err());
        }
        let mut h264 = parser();
        assert!(h264.push(0, &[0x7c, 0x81, 2], false).is_ok());
        assert!(h264.push(1, &[0x7c, 0x41, 2], true).is_err());
        assert!(parser().push(0, &vec![1; MAX_UNIT], true).is_err());
    }

    #[test]
    fn independent_reports_map_wrapped_rtp_clocks_to_one_source_time() {
        let now = ClockTime::now();
        let origin = (u64::MAX - (1 << 31) + 1, now);
        let later = report_time(origin, 1 << 31).unwrap();
        assert_eq!(later - now, Duration::from_secs(1));
        let video = Clock {
            ssrc: Some(1),
            report: Some((u32::MAX - 99, now, now)),
        };
        let mut audio = Clock {
            ssrc: Some(2),
            report: Some((60_000, later, now)),
        };
        assert_eq!(
            video.map(44_900, 90_000).unwrap(),
            audio.map(36_000, 48_000).unwrap()
        );
        assert!(audio.source(3).is_err());
        assert!(video.map(9_000_000, 90_000).is_err());
    }
}
