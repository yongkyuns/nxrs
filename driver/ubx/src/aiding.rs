//! One bounded initialization message, advanced by the existing UART owner.
use crate::{ubx, DriverError};
use peripheral::gnss::aiding::{Aid, Outcome, Progress, SubmitError};
use std::{
    io::{ErrorKind, Write},
    time::{Duration, Instant, UNIX_EPOCH},
};

#[derive(Default)]
pub(super) struct Aiding {
    pending: Option<Pending>,
    // Account for earlier accepted writes still serializing at the configured
    // baud. The transport must be the sole owner of an unthrottled 8N1 UART.
    queued_until: Option<Instant>,
    incomplete: bool,
}

struct Pending {
    aid: Aid,
    frame: ubx::Command,
    written: usize,
    deadline: Instant,
}

impl Aiding {
    pub(super) fn begin(
        &mut self,
        aid: Aid,
        now_us: u64,
        deadline: Instant,
    ) -> Result<(), SubmitError> {
        if self.incomplete {
            return Err(SubmitError::Io);
        }
        if self.pending.is_some() {
            return Err(SubmitError::Busy);
        }
        aid.validate(now_us)?;
        if deadline <= Instant::now() {
            return Err(SubmitError::Expired);
        }
        self.pending = Some(Pending {
            aid,
            frame: encode(aid, now_us, Duration::ZERO)?,
            written: 0,
            deadline,
        });
        Ok(())
    }

    pub(super) fn advance(
        &mut self,
        io: &mut impl Write,
        baud: u32,
        now: Instant,
        now_us: u64,
    ) -> Result<Progress, DriverError> {
        let Some(pending) = self.pending.as_mut() else {
            return Ok(Progress::Idle);
        };
        if now >= pending.deadline {
            let partial = pending.written != 0 && pending.written < pending.frame.len();
            self.pending = None;
            // A truncated outbound UBX frame makes subsequent configuration
            // traffic unsafe. The HAL must retire/quarantine the transport.
            self.incomplete |= partial;
            return if partial {
                Err(DriverError::Io)
            } else {
                Ok(Progress::Complete(Ok(Outcome::TimedOut)))
            };
        }
        if pending.written == 0 {
            // Regenerate up to the first successful write. Once a prefix is on
            // the wire, the rest of that frame must remain unchanged.
            let backlog = self
                .queued_until
                .map_or(Duration::ZERO, |until| until.saturating_duration_since(now));
            let transport = pending
                .deadline
                .duration_since(now)
                .saturating_add(backlog)
                .saturating_add(wire_time(pending.frame.len(), baud));
            pending.frame = match encode(pending.aid, now_us, transport) {
                Ok(frame) => frame,
                Err(error) => {
                    self.pending = None;
                    return Ok(Progress::Complete(Err(error)));
                }
            };
        }
        if pending.written < pending.frame.len() {
            match io.write(&pending.frame[pending.written..]) {
                Ok(0) => return Err(DriverError::Io),
                Ok(count) => {
                    pending.written += count;
                    self.queued_until = Some(
                        self.queued_until
                            .unwrap_or(now)
                            .max(now)
                            .checked_add(wire_time(count, baud))
                            .ok_or(DriverError::Io)?,
                    );
                }
                Err(error)
                    if matches!(error.kind(), ErrorKind::WouldBlock | ErrorKind::Interrupted) => {}
                Err(_) => return Err(DriverError::Io),
            }
            if pending.written < pending.frame.len() {
                return Ok(Progress::Writing);
            }
        }
        // MGA ACKs contain no transaction identity. A delayed ACK from an
        // earlier command/session cannot safely confirm this write.
        self.pending = None;
        Ok(Progress::Complete(Ok(Outcome::Unconfirmed)))
    }

    pub(super) fn cancel(&mut self) -> Result<(), DriverError> {
        if let Some(pending) = self.pending.take() {
            self.incomplete |= pending.written != 0 && pending.written < pending.frame.len();
        }
        if self.incomplete {
            Err(DriverError::Io)
        } else {
            Ok(())
        }
    }
}

fn wire_time(bytes: usize, baud: u32) -> Duration {
    Duration::from_nanos((bytes as u64 * 10 * 1_000_000_000).div_ceil(u64::from(baud.max(1))))
}

fn encode(aid: Aid, now_us: u64, transport: Duration) -> Result<ubx::Command, SubmitError> {
    let latest_us = u64::try_from(transport.as_nanos().div_ceil(1_000))
        .ok()
        .and_then(|delay| now_us.checked_add(delay))
        .ok_or(SubmitError::InvalidInput)?;
    // Freshness and uncertainty must hold through the latest claimed receipt,
    // including partial-write scheduling and complete 8N1 serialization.
    aid.validate(latest_us)?;
    let command = match aid {
        Aid::Time(time) => {
            let (unix_ms, _) = time.at(now_us)?;
            let (_, uncertainty) = time.at(latest_us)?;
            let unix_ms = u64::try_from(unix_ms).map_err(|_| SubmitError::InvalidInput)?;
            let utc = UNIX_EPOCH
                .checked_add(Duration::from_millis(unix_ms))
                .ok_or(SubmitError::InvalidInput)?;
            let accuracy = uncertainty
                .checked_add(transport)
                .and_then(|accuracy| accuracy.checked_add(Duration::from_millis(1)))
                .ok_or(SubmitError::InvalidInput)?;
            ubx::initial_time(utc, accuracy)
        }
        Aid::Position(position) => ubx::initial_position(
            position.latitude_deg,
            position.longitude_deg,
            position.altitude_m,
            position.accuracy_at(latest_us)?,
        ),
    };
    command.map_err(|_| SubmitError::InvalidInput)
}
