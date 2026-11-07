//! GNSS capability policy around the receiver-specific UBX driver.
use crate::{DeviceError, GnssFix};
use nxrs_ubx_driver::{Driver, DriverError};
use std::{
    io::{Read, Write},
    time::{Duration, Instant},
};

const TRANSACTION_BUDGET: Duration = Duration::from_secs(1);

pub(crate) struct Receiver<R> {
    driver: Driver<R>,
    saved_rate: Option<u8>,
    last_epoch: Option<u32>,
    sequence: u64,
}

impl<R: Read + Write> Receiver<R> {
    pub(crate) fn new(io: R) -> Self {
        Self {
            driver: Driver::new(io),
            saved_rate: None,
            last_epoch: None,
            sequence: 0,
        }
    }

    pub(crate) fn io(&self) -> &R {
        self.driver.io()
    }

    pub(crate) fn io_mut(&mut self) -> &mut R {
        self.driver.io_mut()
    }

    pub(crate) fn configure(&mut self) -> Result<(), DeviceError> {
        self.configure_until(Instant::now() + TRANSACTION_BUDGET)
    }

    fn configure_until(&mut self, deadline: Instant) -> Result<(), DeviceError> {
        self.saved_rate = Some(
            self.driver
                .query_nav_pvt_uart1_rate(deadline)
                .map_err(map_error)?,
        );
        self.driver
            .set_and_verify_nav_pvt_uart1_rate(1, deadline)
            .map_err(map_error)?;
        // Do not publish navigation received during acquisition/configuration.
        self.driver.reset_decoder();
        self.sequence = 0;
        self.last_epoch = None;
        Ok(())
    }

    pub(crate) fn restore(&mut self) -> Result<(), DeviceError> {
        self.restore_until(Instant::now() + TRANSACTION_BUDGET)
    }

    fn restore_until(&mut self, deadline: Instant) -> Result<(), DeviceError> {
        if let Some(rate) = self.saved_rate {
            self.driver
                .set_and_verify_nav_pvt_uart1_rate(rate, deadline)
                .map_err(map_error)?;
            self.saved_rate = None;
        }
        Ok(())
    }

    pub(crate) fn read_events(
        &mut self,
        now_ms: u64,
        mut emit: impl FnMut(Result<GnssFix, DeviceError>),
    ) -> Result<(), DeviceError> {
        self.driver
            .poll(|event| {
                let report = match event {
                    Ok(report) => report,
                    Err(error) => return emit(Err(map_error(error))),
                };
                if self.last_epoch == Some(report.itow_ms) {
                    return;
                }
                self.last_epoch = Some(report.itow_ms);
                self.sequence = self.sequence.saturating_add(1);
                emit(Ok(GnssFix {
                    sequence: self.sequence,
                    timestamp_ms: now_ms,
                    latitude_deg: report.latitude_deg,
                    longitude_deg: report.longitude_deg,
                    speed_mps: report.speed_mps,
                    course_rad: report.heading_rad,
                }));
            })
            .map_err(map_error)
    }
}

fn map_error(error: DriverError) -> DeviceError {
    match error {
        DriverError::Io => DeviceError::Io,
        DriverError::Timeout => DeviceError::Timeout,
        DriverError::InvalidData => DeviceError::InvalidData,
        DriverError::NoFix => DeviceError::Busy,
    }
}

#[cfg(test)]
#[path = "tests.rs"]
mod tests;
