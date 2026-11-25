use crate::{Capture, DeviceError, Frame, SAMPLES_PER_BLOCK};
use std::time::{Duration, Instant};

pub(crate) const BUFFER_COUNT: usize = 2;

// I²S provides two little-endian 32-bit slots with the signed PCM in their
// most significant bits. Retain the signed high 16 bits of the chosen slot.
pub(crate) fn decode_slots(
    bytes: &[u8],
    slot: usize,
    samples: &mut [i16; SAMPLES_PER_BLOCK],
) -> Result<(), DeviceError> {
    if slot > 1 || bytes.len() != SAMPLES_PER_BLOCK * 8 {
        return Err(DeviceError::InvalidData);
    }
    let high = slot * 4 + 2;
    for (sample, slots) in samples.iter_mut().zip(bytes.chunks_exact(8)) {
        *sample = i16::from_le_bytes([slots[high], slots[high + 1]]);
    }
    Ok(())
}

// One execution owner performs all source operations.
pub(crate) trait Source {
    fn start(&mut self) -> Result<(), DeviceError>;
    fn submit(&mut self, index: usize) -> Result<(), DeviceError>;
    fn completed(&mut self, until: Instant) -> Result<usize, DeviceError>;
    fn read_samples(
        &self,
        index: usize,
        samples: &mut [i16; SAMPLES_PER_BLOCK],
    ) -> Result<(), DeviceError>;

    // Busy leaves all unfinished resources owned by the source.
    fn retire(&mut self) -> Result<(), DeviceError>;
    fn retain_for_retry(self);
}

pub(crate) struct Session<S: Source> {
    source: Option<S>,
    next_sample: u64,
    retiring: bool,
    fault: Option<DeviceError>,
}

impl<S: Source> Session<S> {
    pub(crate) fn open(source: S) -> Result<Self, DeviceError> {
        let mut session = Self {
            source: Some(source),
            next_sample: 0,
            retiring: false,
            fault: None,
        };
        let source = session
            .source
            .as_mut()
            .expect("new session owns its source");
        source.start()?;
        for index in 0..BUFFER_COUNT {
            source.submit(index)?;
        }
        Ok(session)
    }

    fn read_block(
        &mut self,
        until: Instant,
        samples: &mut [i16; SAMPLES_PER_BLOCK],
    ) -> Result<Frame, DeviceError> {
        let source = self.source.as_mut().ok_or(DeviceError::Closed)?;
        let index = source.completed(until)?;
        let observed_at = Instant::now();
        source.read_samples(index, samples)?;
        source.submit(index)?;
        let first_sample = self.next_sample;
        self.next_sample = self
            .next_sample
            .checked_add(SAMPLES_PER_BLOCK as u64)
            .ok_or(DeviceError::InvalidData)?;
        Ok(Frame {
            first_sample,
            observed_at,
        })
    }
}

impl<S: Source> Capture for Session<S> {
    fn read(
        &mut self,
        until: Instant,
        samples: &mut [i16; SAMPLES_PER_BLOCK],
    ) -> Result<Frame, DeviceError> {
        if self.retiring {
            return Err(DeviceError::Closed);
        }
        if let Some(fault) = self.fault {
            return Err(fault);
        }

        let result = self.read_block(until, samples);
        if let Err(error) = result {
            if error != DeviceError::Timeout {
                self.fault = Some(error);
            }
        }
        result
    }

    fn stop(&mut self, until: Instant) -> Result<(), DeviceError> {
        self.retiring = true;
        let Some(source) = self.source.as_mut() else {
            return Ok(());
        };

        loop {
            match source.retire() {
                Ok(()) => {
                    self.source = None;
                    return Ok(());
                }
                Err(DeviceError::Busy) => {
                    let remaining = until.saturating_duration_since(Instant::now());
                    if remaining.is_zero() {
                        return Err(DeviceError::Timeout);
                    }
                    std::thread::sleep(remaining.min(Duration::from_millis(1)));
                }
                Err(error) => return Err(error),
            }
        }
    }
}

impl<S: Source> Drop for Session<S> {
    fn drop(&mut self) {
        if let Some(mut source) = self.source.take() {
            if source.retire().is_err() {
                // A failed retirement can leave native memory and callbacks
                // live. Keep the source and its exclusive admission together.
                source.retain_for_retry();
            }
        }
    }
}

#[cfg(test)]
mod tests;
