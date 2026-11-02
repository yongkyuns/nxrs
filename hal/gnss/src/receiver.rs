//! Bounded receiver ownership/configuration. UBX wire codecs live in `kinavis-ubx`.
use crate::{DeviceError, Gnss, GnssFix};
use std::{
    io::{ErrorKind, Read, Write},
    time::{Duration, Instant},
};
#[path = "ubx.rs"]
mod ubx;
use ubx::Event;

pub(crate) struct Receiver<R> {
    io: R,
    decoder: ubx::Decoder,
    saved_rate: Option<u8>,
}
impl<R: Read + Write> Receiver<R> {
    pub(crate) fn new(io: R) -> Self {
        Self {
            io,
            decoder: ubx::Decoder::default(),
            saved_rate: None,
        }
    }
    pub(crate) fn configure(&mut self) -> Result<(), DeviceError> {
        self.configure_until(Instant::now() + Duration::from_secs(1))
    }
    fn configure_until(&mut self, deadline: Instant) -> Result<(), DeviceError> {
        self.saved_rate = Some(self.query_rate(deadline)?);
        self.change_rate(1, deadline)?;
        // Do not publish navigation received during acquisition/configuration.
        self.decoder = ubx::Decoder::default();
        Ok(())
    }
    pub(crate) fn restore(&mut self) -> Result<(), DeviceError> {
        self.restore_until(Instant::now() + Duration::from_secs(1))
    }
    fn restore_until(&mut self, deadline: Instant) -> Result<(), DeviceError> {
        if let Some(rate) = self.saved_rate {
            self.change_rate(rate, deadline)?;
            self.saved_rate = None;
        }
        Ok(())
    }
    fn change_rate(&mut self, rate: u8, deadline: Instant) -> Result<(), DeviceError> {
        self.send(&ubx::set_rate(rate), deadline)?;
        self.wait(deadline, |event| match event {
            Event::Ack(true) => Some(Ok(())),
            _ => None,
        })?;
        if self.query_rate(deadline)? != rate {
            return Err(DeviceError::InvalidData);
        }
        Ok(())
    }
    fn query_rate(&mut self, deadline: Instant) -> Result<u8, DeviceError> {
        self.send(&ubx::get_rate(), deadline)?;
        self.wait(deadline, |event| match event {
            Event::Rate(rate) => Some(Ok(rate)),
            _ => None,
        })
    }
    fn send(&mut self, mut bytes: &[u8], deadline: Instant) -> Result<(), DeviceError> {
        while !bytes.is_empty() {
            check_deadline(deadline)?;
            match self.io.write(bytes) {
                Ok(0) => return Err(DeviceError::Io),
                Ok(n) => bytes = &bytes[n..],
                Err(e) if e.kind() == ErrorKind::Interrupted => {}
                Err(e) if e.kind() == ErrorKind::WouldBlock => pause(),
                Err(_) => return Err(DeviceError::Io),
            }
        }
        Ok(())
    }
    fn wait<T>(
        &mut self,
        deadline: Instant,
        select: impl Fn(Event) -> Option<Result<T, DeviceError>>,
    ) -> Result<T, DeviceError> {
        let mut bytes = [0; 128];
        loop {
            check_deadline(deadline)?;
            match self.io.read(&mut bytes) {
                Ok(0) => return Err(DeviceError::Io),
                Ok(n) => {
                    let mut reply = None;
                    self.decoder.consume(&bytes[..n], 0, |event| {
                        if reply.is_none() {
                            reply = match event {
                                Event::Ack(false) => Some(Err(DeviceError::InvalidData)),
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
                Err(_) => return Err(DeviceError::Io),
            }
        }
    }
}
fn check_deadline(deadline: Instant) -> Result<(), DeviceError> {
    if Instant::now() >= deadline {
        Err(DeviceError::Timeout)
    } else {
        Ok(())
    }
}
fn pause() {
    std::thread::sleep(Duration::from_millis(1));
}
impl<R: Read + Write> Gnss for Receiver<R> {
    fn fix(&mut self, now_ms: u64) -> Result<GnssFix, DeviceError> {
        let mut latest = Err(DeviceError::Busy);
        let mut bytes = [0; 128];
        // Drain toward the most recently received record without unbounded work.
        for _ in 0..32 {
            match self.io.read(&mut bytes) {
                Ok(0) => return Err(DeviceError::Io),
                Ok(count) => self
                    .decoder
                    .consume(&bytes[..count], now_ms, |event| match event {
                        Event::Fix(fix) => latest = fix,
                        Event::Invalid => latest = Err(DeviceError::InvalidData),
                        _ => {}
                    }),
                Err(error) if error.kind() == ErrorKind::WouldBlock => return latest,
                Err(error) if error.kind() == ErrorKind::Interrupted => continue,
                Err(_) => return Err(DeviceError::Io),
            }
        }
        // Catch up on the next call instead of presenting an undrained backlog.
        Err(DeviceError::Busy)
    }
}

#[cfg(test)]
#[path = "tests.rs"]
mod tests;
