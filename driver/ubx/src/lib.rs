//! M9 NAV-PVT parsing and RAM NAV-PVT-UART1 configuration over an injected
//! nonblocking byte stream. Device/session policy belongs to the caller.

mod ubx;

use std::{
    io::{ErrorKind, Read, Write},
    time::{Duration, Instant},
};

/// A protocol report without host time, sequence, or replay policy.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct NavPvt {
    /// Receiver time of week in milliseconds.
    pub itow_ms: u32,
    pub latitude_deg: f64,
    pub longitude_deg: f64,
    pub speed_mps: f32,
    pub speed_accuracy_mps: f32,
    /// Present only when the speed and heading accuracy support a usable heading.
    pub heading_rad: Option<f32>,
    pub heading_accuracy_rad: f32,
}

pub use peripheral::gnss::Error as DriverError;

/// A single injected UBX byte stream and its bounded frame decoder.
pub struct Driver<R> {
    io: R,
    decoder: ubx::Decoder,
    saved_rate: Option<u8>,
}

impl<R: Read + Write> Driver<R> {
    pub fn new(io: R) -> Self {
        Self {
            io,
            decoder: ubx::Decoder::default(),
            saved_rate: None,
        }
    }

    pub fn io(&self) -> &R {
        &self.io
    }

    pub fn io_mut(&mut self) -> &mut R {
        &mut self.io
    }

    /// Discard any partial frame retained across `poll` calls.
    pub fn reset_decoder(&mut self) {
        self.decoder = ubx::Decoder::default();
    }

    /// Read at most one chunk. `WouldBlock` and `Interrupted` mean no report is
    /// ready yet; framing state is retained across calls.
    pub fn poll(
        &mut self,
        mut emit: impl FnMut(Result<NavPvt, DriverError>),
    ) -> Result<(), DriverError> {
        let mut bytes = [0; 128];
        match self.io.read(&mut bytes) {
            Ok(0) => Err(DriverError::Io),
            Ok(n) => {
                self.decoder.consume(&bytes[..n], |event| match event {
                    ubx::Event::NavPvt(report) => emit(report),
                    ubx::Event::Invalid => emit(Err(DriverError::InvalidData)),
                    _ => {}
                });
                Ok(())
            }
            Err(e) if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::Interrupted) => Ok(()),
            Err(_) => Err(DriverError::Io),
        }
    }

    /// Query the RAM value of `CFG-MSGOUT-NAV-PVT-UART1`.
    pub fn query_nav_pvt_uart1_rate(&mut self, deadline: Instant) -> Result<u8, DriverError> {
        self.send(&ubx::get_rate(), deadline)?;
        self.wait(deadline, |event| match event {
            ubx::Event::Rate(rate) => Some(Ok(rate)),
            _ => None,
        })
    }

    /// Write the RAM value and wait for the matching positive ACK.
    pub fn set_nav_pvt_uart1_rate(
        &mut self,
        rate: u8,
        deadline: Instant,
    ) -> Result<(), DriverError> {
        self.send(&ubx::set_rate(rate), deadline)?;
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
        self.set_nav_pvt_uart1_rate(rate, deadline)?;
        if self.query_nav_pvt_uart1_rate(deadline)? != rate {
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
                    self.decoder.consume(&bytes[..n], |event| {
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
        self.set_and_verify_nav_pvt_uart1_rate(1, deadline)?;
        self.reset_decoder();
        Ok(())
    }
    fn restore(&mut self, deadline: Instant) -> Result<(), DriverError> {
        if let Some(rate) = self.saved_rate {
            self.set_and_verify_nav_pvt_uart1_rate(rate, deadline)?;
            self.saved_rate = None;
        }
        Ok(())
    }
    fn read_available(
        &mut self,
        mut emit: impl FnMut(Result<peripheral::gnss::Measurement, DriverError>),
    ) -> Result<(), DriverError> {
        self.poll(|event| {
            emit(event.map(|report| peripheral::gnss::Measurement {
                epoch_ms: u64::from(report.itow_ms),
                latitude_deg: report.latitude_deg,
                longitude_deg: report.longitude_deg,
                speed_mps: report.speed_mps,
                course_rad: report.heading_rad,
            }))
        })
    }
}
