//! Fixed display and communications readers of the latest sensor observations.

use super::{
    sample_timing, Input, Measurement, Observation, Reading, SampleTiming, SensorFault, Snapshot,
    Source,
};
use std::sync::{
    atomic::{AtomicU32, AtomicU8, Ordering},
    Arc,
};
use std::time::{Duration, Instant};
use triple_buffer::{triple_buffer, Input as WriteSlot, Output};

struct Faults<const IMUS: usize> {
    imu: [AtomicU8; IMUS],
    gnss: AtomicU8,
    invalid_frames: AtomicU32,
}

impl<const IMUS: usize> Faults<IMUS> {
    fn mark(&self, fault: SensorFault) -> bool {
        let (flag, code) = match fault {
            SensorFault::Imu(index, error) => (&self.imu[index], imu_code(error)),
            SensorFault::Gnss(error) => (&self.gnss, gnss_code(error)),
        };
        flag.compare_exchange(0, code, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
    }
}

// Capability-local errors retain their own contracts. These private encodings
// only retain a first cause in fixed storage; they are not a public wire ABI.
fn imu_code(error: imu::DeviceError) -> u8 {
    match error {
        imu::DeviceError::Unsupported => 1,
        imu::DeviceError::Busy => 2,
        imu::DeviceError::Timeout => 3,
        imu::DeviceError::Full => 4,
        imu::DeviceError::Io => 5,
        imu::DeviceError::InvalidData => 6,
    }
}
fn imu_error(code: u8) -> Option<imu::DeviceError> {
    match code {
        1 => Some(imu::DeviceError::Unsupported),
        2 => Some(imu::DeviceError::Busy),
        3 => Some(imu::DeviceError::Timeout),
        4 => Some(imu::DeviceError::Full),
        5 => Some(imu::DeviceError::Io),
        6 => Some(imu::DeviceError::InvalidData),
        _ => None,
    }
}
fn gnss_code(error: gnss::DeviceError) -> u8 {
    match error {
        gnss::DeviceError::Unsupported => 1,
        gnss::DeviceError::Busy => 2,
        gnss::DeviceError::Timeout => 3,
        gnss::DeviceError::Full => 4,
        gnss::DeviceError::Io => 5,
        gnss::DeviceError::InvalidData => 6,
    }
}
fn gnss_error(code: u8) -> Option<gnss::DeviceError> {
    match code {
        1 => Some(gnss::DeviceError::Unsupported),
        2 => Some(gnss::DeviceError::Busy),
        3 => Some(gnss::DeviceError::Timeout),
        4 => Some(gnss::DeviceError::Full),
        5 => Some(gnss::DeviceError::Io),
        6 => Some(gnss::DeviceError::InvalidData),
        _ => None,
    }
}

struct Pair<T: Copy + Send>([WriteSlot<Source<T>>; 2], Source<T>);

impl<T: Copy + Send> Pair<T> {
    fn new(fresh_for: Duration) -> (Self, [Output<Source<T>>; 2]) {
        let initial = Source::new(fresh_for);
        let (left, display) = triple_buffer(&initial);
        let (right, links) = triple_buffer(&initial);
        (Self([left, right], initial), [display, links])
    }

    fn publish(&mut self, sample: T, received: Instant, timing: SampleTiming) {
        self.1.receive(sample, received, timing);
        for input in &mut self.0 {
            input.write(self.1);
        }
    }

    fn unavailable(&mut self, received: Instant) {
        self.1.unavailable(received);
        for input in &mut self.0 {
            input.write(self.1);
        }
    }
}

enum SourceSlots {
    Imu(usize, Pair<imu::ImuSample>),
    Gnss(
        Pair<gnss::GnssFix>,
        WriteSlot<Source<gnss::SatelliteReport>>,
    ),
}

/// Owned by the callback of one acquisition session.
pub struct Publisher<const IMUS: usize> {
    slots: SourceSlots,
    faults: Arc<Faults<IMUS>>,
}

impl<const IMUS: usize> Publisher<IMUS> {
    /// Publish before sending a wake or handing the sample to an ordered consumer.
    ///
    /// The observation must belong to this publisher's acquisition session.
    pub fn record(
        &mut self,
        observation: &Observation,
    ) -> Result<Option<Measurement>, SensorFault> {
        let received = observation.received;
        let fault = match (&mut self.slots, &observation.input) {
            (SourceSlots::Imu(index, slots), Input::Imu(source, event)) if index == source => {
                if self.faults.imu[*index].load(Ordering::Acquire) != 0 {
                    return Ok(None);
                }
                match *event {
                    imu::Event::Sample(value) => {
                        slots.publish(
                            value,
                            received,
                            sample_timing(
                                value.timestamp_us,
                                value.received_us,
                                value.is_capture_time,
                            ),
                        );
                        return Ok(observation.measurement());
                    }
                    imu::Event::Failed(error) => SensorFault::Imu(*index, error),
                }
            }
            (SourceSlots::Gnss(fixes, satellites), Input::Gnss(event)) => {
                if self.faults.gnss.load(Ordering::Acquire) != 0 {
                    return Ok(None);
                }
                match *event {
                    gnss::Event::Fix(value) => {
                        fixes.publish(
                            value,
                            received,
                            sample_timing(
                                value.timestamp_us,
                                value.received_us,
                                value.is_capture_time,
                            ),
                        );
                        return Ok(observation.measurement());
                    }
                    gnss::Event::NoFix(_) => {
                        fixes.unavailable(received);
                        return Ok(None);
                    }
                    gnss::Event::Satellites(report) => {
                        let mut value = Source::new(Duration::from_millis(
                            2 * u64::from(report.interval_ms.max(3_000)),
                        ));
                        value.receive(report, received, SampleTiming::Receipt);
                        satellites.write(value);
                        return Ok(None);
                    }
                    gnss::Event::InvalidFrame => {
                        // One GNSS publisher owns this counter; readers only load it.
                        let count = self.faults.invalid_frames.load(Ordering::Relaxed);
                        self.faults
                            .invalid_frames
                            .store(count.saturating_add(1), Ordering::Relaxed);
                        return Ok(None);
                    }
                    gnss::Event::Failed(error) => SensorFault::Gnss(error),
                }
            }
            _ => panic!("observation publisher used with another source"),
        };
        if self.faults.mark(fault) {
            Err(fault)
        } else {
            Ok(None)
        }
    }
}

/// One independent polling reader. Each source is complete; sources may
/// represent different instants. Only the display subscribes to satellites.
pub struct FixedReader<const IMUS: usize> {
    imu: [Output<Source<imu::ImuSample>>; IMUS],
    gnss: Output<Source<gnss::GnssFix>>,
    satellites: Option<Output<Source<gnss::SatelliteReport>>>,
    faults: Arc<Faults<IMUS>>,
}

impl<const IMUS: usize> FixedReader<IMUS> {
    /// First terminal cause for this acquisition lifetime, independent of wake delivery.
    pub fn faults(&self) -> ([Option<imu::DeviceError>; IMUS], Option<gnss::DeviceError>) {
        (
            std::array::from_fn(|index| imu_error(self.faults.imu[index].load(Ordering::Acquire))),
            gnss_error(self.faults.gnss.load(Ordering::Acquire)),
        )
    }

    /// Rejected GNSS frames/epochs, saturated rather than silently discarded.
    pub fn gnss_invalid_frames(&self) -> u32 {
        self.faults.invalid_frames.load(Ordering::Relaxed)
    }
    /// Historical last accepted fix. This does not imply present availability;
    /// use `snapshot` for currentness and retain this fix's time/provenance.
    pub fn last_gnss(&mut self) -> Option<gnss::GnssFix> {
        self.gnss.read().last
    }
    pub fn snapshot(&mut self, now: Instant) -> Snapshot<IMUS> {
        Snapshot {
            imu: std::array::from_fn(|index| {
                read(&mut self.imu[index], &self.faults.imu[index], now)
            }),
            gnss: Some(read(&mut self.gnss, &self.faults.gnss, now)),
        }
    }

    pub fn satellites(&mut self, now: Instant) -> Reading<gnss::SatelliteReport> {
        self.satellites
            .as_mut()
            .map_or(Reading::Waiting, |slot| read(slot, &self.faults.gnss, now))
    }

    /// Retain a start or joined failure even if the worker never notified.
    pub fn fail(&self, fault: SensorFault) {
        self.faults.mark(fault);
    }
}

fn read<T: Copy + Send>(
    slot: &mut Output<Source<T>>,
    fault: &AtomicU8,
    now: Instant,
) -> Reading<T> {
    let value = slot.read();
    if fault.load(Ordering::Acquire) != 0 {
        Reading::Fault
    } else {
        value.reading(now)
    }
}

/// Reserve two fixed subscriptions and all producer storage before opening the
/// sensor sessions. A reader can attach later and see the newest publication.
pub fn fixed_readers<const IMUS: usize>(
    imu_fresh_for: Duration,
    gnss_fresh_for: Duration,
) -> (
    [Publisher<IMUS>; IMUS],
    Publisher<IMUS>,
    [FixedReader<IMUS>; 2],
) {
    let faults = Arc::new(Faults {
        imu: std::array::from_fn(|_| AtomicU8::new(0)),
        gnss: AtomicU8::new(0),
        invalid_frames: AtomicU32::new(0),
    });
    let (gnss, [display_fix, links_fix]) = Pair::new(gnss_fresh_for);
    let (satellites, display_satellites) = triple_buffer(&Source::new(Duration::from_secs(6)));
    let mut display = std::array::from_fn(|_| None);
    let mut links = std::array::from_fn(|_| None);
    let imus = std::array::from_fn(|index| {
        let (pair, [display_slot, links_slot]) = Pair::new(imu_fresh_for);
        display[index] = Some(display_slot);
        links[index] = Some(links_slot);
        Publisher {
            slots: SourceSlots::Imu(index, pair),
            faults: faults.clone(),
        }
    });
    (
        imus,
        Publisher {
            slots: SourceSlots::Gnss(gnss, satellites),
            faults: faults.clone(),
        },
        [
            FixedReader {
                imu: display.map(Option::unwrap),
                gnss: display_fix,
                satellites: Some(display_satellites),
                faults: faults.clone(),
            },
            FixedReader {
                imu: links.map(Option::unwrap),
                gnss: links_fix,
                satellites: None,
                faults,
            },
        ],
    )
}
