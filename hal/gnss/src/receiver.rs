//! Stream metadata around a normalized synchronous receiver.
use crate::{DeviceError, GnssFix};
use peripheral::gnss::Receiver as Protocol;

pub(crate) struct Receiver<R> {
    pub(crate) source: R,
    last_epoch: Option<u64>,
    sequence: u64,
}
impl<R: Protocol> Receiver<R> {
    pub(crate) fn new(source: R) -> Self {
        Self {
            source,
            last_epoch: None,
            sequence: 0,
        }
    }
    pub(crate) fn read_events(
        &mut self,
        now_ms: u64,
        mut emit: impl FnMut(Result<GnssFix, DeviceError>),
    ) -> Result<(), DeviceError> {
        self.source
            .read_available(|event| {
                let report = match event {
                    Ok(report) => report,
                    Err(error) => return emit(Err(map_error(error))),
                };
                if self.last_epoch == Some(report.epoch_ms) {
                    return;
                }
                self.last_epoch = Some(report.epoch_ms);
                self.sequence = self.sequence.saturating_add(1);
                emit(Ok(GnssFix {
                    sequence: self.sequence,
                    timestamp_ms: now_ms,
                    latitude_deg: report.latitude_deg,
                    longitude_deg: report.longitude_deg,
                    speed_mps: report.speed_mps,
                    course_rad: report.course_rad,
                }));
            })
            .map_err(map_error)
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
