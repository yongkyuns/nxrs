//! Stream metadata around a normalized synchronous receiver.
use crate::{
    Constellation, DeviceError, Event, FixQuality, GnssFix, SatelliteReport, SatelliteSignal,
    SatelliteStatus,
};
use peripheral::gnss::{Receiver as Protocol, Report};

pub(crate) struct Receiver<R> {
    pub(crate) source: R,
    last_epoch: Option<u64>,
    sequence: u64,
    last_timestamp: Option<u64>,
    pulse: crate::pps::Sync,
}
impl<R: Protocol> Receiver<R> {
    pub(crate) fn new(source: R) -> Self {
        Self {
            source,
            last_epoch: None,
            sequence: 0,
            last_timestamp: None,
            pulse: crate::pps::Sync::default(),
        }
    }
    pub(crate) fn observe_edge(&mut self, sequence: usize, time_us: u64) {
        self.pulse.edge(sequence, time_us);
    }
    pub(crate) fn read_events(
        &mut self,
        now_us: u64,
        mut emit: impl FnMut(Result<Event, DeviceError>),
    ) -> Result<(), DeviceError> {
        self.source
            .read_available(|event| {
                let report = match event {
                    Ok(Report::Fix(report)) => report,
                    Ok(Report::Pulse(pulse)) => {
                        self.pulse.report(pulse, now_us);
                        return;
                    }
                    Ok(Report::Satellites(report)) => {
                        emit(Ok(Event::Satellites(SatelliteReport::new(
                            report.status.visible,
                            report.status.tracked,
                            report.status.mean_cn0_db_hz,
                            report.interval_ms,
                            report.measured,
                            report.signals.iter().map(|signal| SatelliteSignal {
                                constellation: constellation(signal.constellation),
                                id: signal.id,
                                cn0_db_hz: signal.cn0_db_hz,
                            }),
                        ))));
                        return;
                    }
                    Err(peripheral::gnss::Error::NoFix) => return emit(Ok(Event::NoFix)),
                    Err(error) => return emit(Err(map_error(error))),
                };
                if self.last_epoch == Some(report.epoch_ms) {
                    return;
                }
                self.last_epoch = Some(report.epoch_ms);
                self.sequence = self.sequence.saturating_add(1);
                let capture = self
                    .pulse
                    .fix_time(&report, now_us)
                    .filter(|time| self.last_timestamp.is_none_or(|previous| *time > previous));
                let timestamp_us = capture.unwrap_or(now_us);
                self.last_timestamp = Some(timestamp_us);
                emit(Ok(Event::Fix(GnssFix {
                    sequence: self.sequence,
                    timestamp_us,
                    is_capture_time: capture.is_some(),
                    receiver_utc_ms: report.receiver_utc_ms,
                    latitude_deg: report.latitude_deg,
                    longitude_deg: report.longitude_deg,
                    speed_mps: report.speed_mps,
                    course_rad: report.course_rad,
                    quality: report.quality.map(|quality| FixQuality {
                        satellites_used: quality.satellites_used,
                        horizontal_accuracy_m: quality.horizontal_accuracy_m,
                        vertical_accuracy_m: quality.vertical_accuracy_m,
                        speed_accuracy_mps: quality.speed_accuracy_mps,
                        position_dop: quality.position_dop,
                        satellites: quality.satellites.map(|status| SatelliteStatus {
                            visible: status.visible,
                            tracked: status.tracked,
                            mean_cn0_db_hz: status.mean_cn0_db_hz,
                            age_ms: status.age_ms,
                        }),
                    }),
                })));
            })
            .map_err(map_error)
    }
}

fn constellation(value: peripheral::gnss::Constellation) -> Constellation {
    use peripheral::gnss::Constellation as Raw;
    match value {
        Raw::Gps => Constellation::Gps,
        Raw::Galileo => Constellation::Galileo,
        Raw::Beidou => Constellation::Beidou,
        Raw::Glonass => Constellation::Glonass,
        Raw::Sbas => Constellation::Sbas,
        Raw::Qzss => Constellation::Qzss,
        Raw::Navic => Constellation::Navic,
        Raw::Other => Constellation::Other,
    }
}

pub(crate) fn map_error(error: peripheral::gnss::Error) -> DeviceError {
    match error {
        peripheral::gnss::Error::Io => DeviceError::Io,
        peripheral::gnss::Error::Timeout => DeviceError::Timeout,
        peripheral::gnss::Error::InvalidData => DeviceError::InvalidData,
        peripheral::gnss::Error::NoFix => DeviceError::Busy,
    }
}

#[cfg(test)]
#[path = "tests.rs"]
mod tests;
