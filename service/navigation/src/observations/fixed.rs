//! Fixed display and communications readers of the latest sensor observations.

use super::{Input, Measurement, Observation, Reading, SensorFault, Snapshot};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::time::{Duration, Instant};
use triple_buffer::{triple_buffer, Input as WriteSlot, Output};

struct Faults<const IMUS: usize> {
    imu: [AtomicBool; IMUS],
    gnss: AtomicBool,
}

impl<const IMUS: usize> Faults<IMUS> {
    fn mark(&self, fault: SensorFault) -> bool {
        let flag = match fault {
            SensorFault::Imu(index, _) => &self.imu[index],
            SensorFault::Gnss(_) => &self.gnss,
        };
        !flag.swap(true, Ordering::AcqRel)
    }
}

#[derive(Clone, Copy)]
struct Sample<T> {
    value: T,
    received: Instant,
}

// Keep freshness at the reader and initialize the shared storage with None.
// Copying the initialized Source into the padded buffers triggered Xtensa
// stack realignment when the pinned compiler inlined tracker startup.
type Slot<T> = Option<Sample<T>>;

struct Pair<T: Copy + Send>([WriteSlot<Slot<T>>; 2]);

impl<T: Copy + Send> Pair<T> {
    fn new() -> (Self, [Output<Slot<T>>; 2]) {
        let (left, display) = triple_buffer(&None);
        let (right, links) = triple_buffer(&None);
        (Self([left, right]), [display, links])
    }

    fn publish(&mut self, sample: T, received: Instant) {
        for input in &mut self.0 {
            input.write(Some(Sample {
                value: sample,
                received,
            }));
        }
    }
}

enum SourceSlots {
    Imu(usize, Pair<imu::ImuSample>),
    Gnss(Pair<gnss::GnssFix>, WriteSlot<Slot<gnss::SatelliteReport>>),
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
                match *event {
                    imu::Event::Sample(value) => {
                        slots.publish(value, received);
                        return Ok(observation.measurement());
                    }
                    imu::Event::Failed(error) => SensorFault::Imu(*index, error),
                }
            }
            (SourceSlots::Gnss(fixes, satellites), Input::Gnss(event)) => match *event {
                gnss::Event::Fix(value) => {
                    fixes.publish(value, received);
                    return Ok(observation.measurement());
                }
                gnss::Event::Satellites(report) => {
                    satellites.write(Some(Sample {
                        value: report,
                        received,
                    }));
                    return Ok(None);
                }
                gnss::Event::InvalidFrame => return Ok(None),
                gnss::Event::Failed(error) => SensorFault::Gnss(error),
            },
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
    imu: [Output<Slot<imu::ImuSample>>; IMUS],
    gnss: Output<Slot<gnss::GnssFix>>,
    satellites: Option<Output<Slot<gnss::SatelliteReport>>>,
    imu_fresh_for: Duration,
    gnss_fresh_for: Duration,
    faults: Arc<Faults<IMUS>>,
}

impl<const IMUS: usize> FixedReader<IMUS> {
    pub fn snapshot(&mut self, now: Instant) -> Snapshot<IMUS> {
        Snapshot {
            imu: std::array::from_fn(|index| {
                read(&mut self.imu[index], &self.faults.imu[index], now, |_| {
                    self.imu_fresh_for
                })
            }),
            gnss: Some(read(&mut self.gnss, &self.faults.gnss, now, |_| {
                self.gnss_fresh_for
            })),
        }
    }

    pub fn satellites(&mut self, now: Instant) -> Reading<gnss::SatelliteReport> {
        self.satellites.as_mut().map_or(Reading::Waiting, |slot| {
            read(slot, &self.faults.gnss, now, |report| {
                Duration::from_millis(2 * u64::from(report.interval_ms.max(3_000)))
            })
        })
    }

    /// Retain a start or joined failure even if the worker never notified.
    pub fn fail(&self, fault: SensorFault) {
        self.faults.mark(fault);
    }
}

fn read<T: Copy + Send>(
    slot: &mut Output<Slot<T>>,
    fault: &AtomicBool,
    now: Instant,
    fresh_for: impl FnOnce(&T) -> Duration,
) -> Reading<T> {
    let sample = *slot.read();
    if fault.load(Ordering::Acquire) {
        return Reading::Fault;
    }
    match sample {
        None => Reading::Waiting,
        Some(Sample { value, received })
            if now.saturating_duration_since(received) < fresh_for(&value) =>
        {
            Reading::Live(value)
        }
        Some(_) => Reading::Stale,
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
        imu: std::array::from_fn(|_| AtomicBool::new(false)),
        gnss: AtomicBool::new(false),
    });
    let (gnss, [display_fix, links_fix]) = Pair::new();
    let (satellites, display_satellites) = triple_buffer(&None);
    let mut display = std::array::from_fn(|_| None);
    let mut links = std::array::from_fn(|_| None);
    let imus = std::array::from_fn(|index| {
        let (pair, [display_slot, links_slot]) = Pair::new();
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
                imu_fresh_for,
                gnss_fresh_for,
                faults: faults.clone(),
            },
            FixedReader {
                imu: links.map(Option::unwrap),
                gnss: links_fix,
                satellites: None,
                imu_fresh_for,
                gnss_fresh_for,
                faults,
            },
        ],
    )
}
