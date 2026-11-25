//! M9 navigation reports and RAM message configuration over an injected
//! nonblocking byte stream. Device/session policy belongs to the caller.

mod framing;
mod ubx;

use kinavis_ubx::ConfigKey;
use peripheral::gnss::{FixQuality, Measurement, Report, SatelliteStatus};
use std::{
    io::{ErrorKind, Read, Write},
    time::{Duration, Instant},
};

const MIN_SATELLITE_INTERVAL_MS: u32 = 3_000;

/// A protocol report without host time, sequence, or replay policy.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct NavPvt {
    /// Receiver time of week in milliseconds.
    pub itow_ms: u32,
    pub receiver_utc_ms: Option<i64>,
    pub latitude_deg: f64,
    pub longitude_deg: f64,
    pub speed_mps: f32,
    pub speed_accuracy_mps: f32,
    pub horizontal_accuracy_m: f32,
    pub vertical_accuracy_m: f32,
    pub position_dop: Option<f32>,
    pub satellites_used: u8,
    pub satellites: Option<SatelliteStatus>,
    /// Present only when the speed and heading accuracy support a usable heading.
    pub heading_rad: Option<f32>,
    pub heading_accuracy_rad: f32,
}

pub use peripheral::gnss::Error as DriverError;

/// M9N receiver type exported for the board binding.
pub type NeoM9n<T> = Driver<T>;

/// A single injected UBX byte stream and its bounded frame decoder.
pub struct Driver<R> {
    io: R,
    decoder: ubx::Decoder,
    uart_baud: u32,
    saved_rate: Option<u8>,
    saved_satellite_rate: Option<u8>,
    saved_pulse_rate: Option<u8>,
    navigation_period_ms: Option<u16>,
    saved_navigation: Option<(u16, u16)>,
}

impl<R: Read + Write> Driver<R> {
    pub fn new(io: R) -> Self {
        Self {
            io,
            decoder: ubx::Decoder::default(),
            uart_baud: 38_400,
            saved_rate: None,
            saved_satellite_rate: None,
            saved_pulse_rate: None,
            navigation_period_ms: None,
            saved_navigation: None,
        }
    }

    /// Configure the navigation period in receiver RAM for each active session.
    pub fn with_navigation_period(mut self, milliseconds: u16) -> Self {
        self.navigation_period_ms = Some(milliseconds);
        self
    }

    /// Use the selected UART's baud rate when budgeting satellite reports.
    pub fn with_uart_baud(mut self, baud: u32) -> Self {
        self.uart_baud = baud;
        self
    }

    fn query_navigation(&mut self, deadline: Instant) -> Result<(u16, u16), DriverError> {
        self.send(&ubx::get_navigation(), deadline)?;
        self.wait(deadline, |event| match event {
            ubx::Event::Navigation {
                measurement,
                cycles,
            } => Some(Ok((measurement, cycles))),
            _ => None,
        })
    }

    fn set_navigation(&mut self, values: (u16, u16), deadline: Instant) -> Result<(), DriverError> {
        self.send(&ubx::set_navigation(values.0, values.1), deadline)?;
        self.wait(deadline, |event| match event {
            ubx::Event::Ack(true) => Some(Ok(())),
            _ => None,
        })?;
        if self.query_navigation(deadline)? != values {
            return Err(DriverError::InvalidData);
        }
        Ok(())
    }

    pub fn io(&self) -> &R {
        &self.io
    }

    pub fn io_mut(&mut self) -> &mut R {
        &mut self.io
    }

    /// Discard any partial frame retained across `poll` calls.
    pub fn reset_decoder(&mut self) {
        self.decoder.reset();
    }

    /// Read at most one chunk. `WouldBlock` and `Interrupted` mean no report is
    /// ready yet; framing state is retained across calls.
    pub fn poll(
        &mut self,
        mut emit: impl FnMut(Result<NavPvt, DriverError>),
    ) -> Result<(), DriverError> {
        self.read_chunk(|event| match event {
            ubx::Event::NavPvt(report) => emit(report),
            ubx::Event::Invalid => emit(Err(DriverError::InvalidData)),
            _ => {}
        })
    }

    fn read_chunk(&mut self, mut emit: impl FnMut(ubx::Event)) -> Result<(), DriverError> {
        let mut bytes = [0; 128];
        match self.io.read(&mut bytes) {
            Ok(0) => Err(DriverError::Io),
            Ok(n) => {
                self.decoder.consume(&bytes[..n], &mut emit);
                Ok(())
            }
            Err(e) if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::Interrupted) => Ok(()),
            Err(_) => Err(DriverError::Io),
        }
    }

    /// Query the RAM value of `CFG-MSGOUT-NAV-PVT-UART1`.
    pub fn query_nav_pvt_uart1_rate(&mut self, deadline: Instant) -> Result<u8, DriverError> {
        self.query_rate(ConfigKey::MSGOUT_NAV_PVT_UART1, deadline)
    }

    fn query_rate(&mut self, key: ConfigKey, deadline: Instant) -> Result<u8, DriverError> {
        self.send(&ubx::get_rate(key), deadline)?;
        self.wait(deadline, |event| match event {
            ubx::Event::Rate(actual, rate) if actual == key => Some(Ok(rate)),
            _ => None,
        })
    }

    /// Write the RAM value and wait for the matching positive ACK.
    pub fn set_nav_pvt_uart1_rate(
        &mut self,
        rate: u8,
        deadline: Instant,
    ) -> Result<(), DriverError> {
        self.set_rate(ConfigKey::MSGOUT_NAV_PVT_UART1, rate, deadline)
    }

    fn set_rate(&mut self, key: ConfigKey, rate: u8, deadline: Instant) -> Result<(), DriverError> {
        self.send(&ubx::set_rate(key, rate), deadline)?;
        self.wait(deadline, |event| match event {
            ubx::Event::Ack(true) => Some(Ok(())),
            _ => None,
        })
    }

    /// Write, await ACK, then query RAM to confirm the applied value.
    pub fn set_and_verify_nav_pvt_uart1_rate(
        &mut self,
        rate: u8,
        deadline: Instant,
    ) -> Result<(), DriverError> {
        self.set_and_verify_rate(ConfigKey::MSGOUT_NAV_PVT_UART1, rate, deadline)
    }

    fn set_and_verify_rate(
        &mut self, key: ConfigKey, rate: u8, deadline: Instant,
    ) -> Result<(), DriverError> {
        self.set_rate(key, rate, deadline)?;
        if self.query_rate(key, deadline)? != rate {
            return Err(DriverError::InvalidData);
        }
        Ok(())
    }

    fn send(&mut self, mut bytes: &[u8], deadline: Instant) -> Result<(), DriverError> {
        while !bytes.is_empty() {
            check_deadline(deadline)?;
            match self.io.write(bytes) {
                Ok(0) => return Err(DriverError::Io),
                Ok(n) => bytes = &bytes[n..],
                Err(e) if e.kind() == ErrorKind::Interrupted => {}
                Err(e) if e.kind() == ErrorKind::WouldBlock => pause(),
                Err(_) => return Err(DriverError::Io),
            }
        }
        Ok(())
    }

    fn wait<T>(
        &mut self,
        deadline: Instant,
        select: impl Fn(ubx::Event) -> Option<Result<T, DriverError>>,
    ) -> Result<T, DriverError> {
        let mut bytes = [0; 128];
        loop {
            check_deadline(deadline)?;
            match self.io.read(&mut bytes) {
                Ok(0) => return Err(DriverError::Io),
                Ok(n) => {
                    let mut reply = None;
                    self.decoder.consume(&bytes[..n], &mut |event| {
                        if reply.is_none() {
                            reply = match event {
                                ubx::Event::Ack(false) => Some(Err(DriverError::InvalidData)),
                                _ => select(event),
                            };
                        }
                    });
                    if let Some(reply) = reply {
                        return reply;
                    }
                }
                Err(e) if e.kind() == ErrorKind::Interrupted => {}
                Err(e) if e.kind() == ErrorKind::WouldBlock => pause(),
                Err(_) => return Err(DriverError::Io),
            }
        }
    }
}

fn check_deadline(deadline: Instant) -> Result<(), DriverError> {
    if Instant::now() >= deadline {
        Err(DriverError::Timeout)
    } else {
        Ok(())
    }
}

fn pause() {
    std::thread::sleep(Duration::from_millis(1));
}

fn satellite_report_rate(baud: u32, period_ms: u32) -> Option<u8> {
    // 8N1, with 100 bytes/s reserved for timing and control. Keep the
    // remaining budget in thousandths of a byte per navigation epoch.
    let capacity = (baud.saturating_sub(1_000) / 10).saturating_mul(period_ms);
    let available = capacity.checked_sub(100 * 1_000)?; // NAV-PVT plus framing.
    if available == 0 {
        return None;
    }
    let wire_epochs = (u32::from(framing::MAX_SAT_FRAME_BYTES) * 1_000).div_ceil(available);
    let interval_epochs = MIN_SATELLITE_INTERVAL_MS.div_ceil(period_ms);
    u8::try_from(wire_epochs.max(interval_epochs)).ok()
}

#[cfg(test)]
mod tests;

impl<R> AsRef<R> for Driver<R> {
    fn as_ref(&self) -> &R {
        &self.io
    }
}
impl<R> AsMut<R> for Driver<R> {
    fn as_mut(&mut self) -> &mut R {
        &mut self.io
    }
}
impl<R: Read + Write> peripheral::gnss::Receiver for Driver<R> {
    fn configure(&mut self, deadline: Instant) -> Result<(), DriverError> {
        self.saved_rate = Some(self.query_nav_pvt_uart1_rate(deadline)?);
        let current = self.query_navigation(deadline)?;
        let (measurement, cycles) = if let Some(period) = self.navigation_period_ms {
            self.saved_navigation = Some(current);
            self.set_navigation((period, 1), deadline)?;
            (period, 1)
        } else {
            current
        };
        let nav_period = u32::from(measurement) * u32::from(cycles);
        if nav_period == 0 {
            return Err(DriverError::InvalidData);
        }
        self.set_and_verify_nav_pvt_uart1_rate(1, deadline)?;
        if let Some(rate) = satellite_report_rate(self.uart_baud, nav_period) {
            let key = ConfigKey::MSGOUT_NAV_SAT_UART1;
            self.saved_satellite_rate = Some(self.query_rate(key, deadline)?);
            self.set_and_verify_rate(key, rate, deadline)?;
            self.decoder.set_satellite_age(nav_period.saturating_mul(u32::from(rate)));
        }
        self.reset_decoder();
        Ok(())
    }
    fn enable_time_pulse(&mut self, deadline: Instant) -> Result<(), DriverError> {
        self.saved_pulse_rate = Some(self.query_rate(ubx::TIM_TP_RATE, deadline)?);
        self.set_and_verify_rate(ubx::TIM_TP_RATE, 1, deadline)?;
        self.reset_decoder();
        Ok(())
    }
    fn restore(&mut self, deadline: Instant) -> Result<(), DriverError> {
        let satellites = self.saved_satellite_rate.map_or(Ok(()), |rate| {
            self.set_and_verify_rate(ConfigKey::MSGOUT_NAV_SAT_UART1, rate, deadline)?;
            self.saved_satellite_rate = None;
            Ok(())
        });
        let pulse = self.saved_pulse_rate.map_or(Ok(()), |rate| {
            self.set_and_verify_rate(ubx::TIM_TP_RATE, rate, deadline)?;
            self.saved_pulse_rate = None;
            Ok(())
        });
        let rate = self.saved_rate.map_or(Ok(()), |rate| {
            self.set_and_verify_nav_pvt_uart1_rate(rate, deadline)?;
            self.saved_rate = None;
            Ok(())
        });
        let navigation = self.saved_navigation.map_or(Ok(()), |values| {
            self.set_navigation(values, deadline)?;
            self.saved_navigation = None;
            Ok(())
        });
        satellites.and(pulse).and(rate).and(navigation)
    }
    fn read_available(
        &mut self,
        mut emit: impl FnMut(Result<Report, DriverError>),
    ) -> Result<(), DriverError> {
        self.read_chunk(|event| match event {
            ubx::Event::NavPvt(result) => emit(result.map(|report| {
                Report::Fix(Measurement {
                    epoch_ms: u64::from(report.itow_ms),
                    receiver_utc_ms: report.receiver_utc_ms,
                    latitude_deg: report.latitude_deg,
                    longitude_deg: report.longitude_deg,
                    speed_mps: report.speed_mps,
                    course_rad: report.heading_rad,
                    quality: Some(FixQuality {
                        satellites_used: report.satellites_used,
                        horizontal_accuracy_m: report.horizontal_accuracy_m,
                        vertical_accuracy_m: report.vertical_accuracy_m,
                        speed_accuracy_mps: report.speed_accuracy_mps,
                        position_dop: report.position_dop,
                        satellites: report.satellites,
                    }),
                })
            })),
            ubx::Event::Pulse(pulse) => emit(Ok(Report::Pulse(pulse))),
            ubx::Event::Invalid => emit(Err(DriverError::InvalidData)),
            _ => {}
        })
    }
}
