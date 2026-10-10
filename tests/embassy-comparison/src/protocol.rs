use crate::packet_service;

pub const PRODUCERS: usize = 4;
pub const WORKERS: usize = 15;
pub const LANES_PER_WORKER: usize = 3;
pub const LANES: usize = WORKERS * LANES_PER_WORKER;
pub const QUEUES: usize = LANES + WORKERS;
pub const SEQUENCES: u16 = 32;
pub const SAMPLE_EVERY: u16 = 8;
pub const SAMPLES: usize = LANES * PRODUCERS * (SEQUENCES as usize / SAMPLE_EVERY as usize);
pub const CYCLES_PER_US: u32 = 240;
pub const EXPECTED_DIGEST: u32 = 441_445_568;
const PAYLOAD_BYTES: usize = 236;
const CHECKSUM_SALT: u32 = 0xa5a5_a5a5;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(C)]
pub struct Event {
    pub lane: u8,
    pub producer: u8,
    pub sequence: u16,
    pub sent_cycles: u32,
    pub checksum: u32,
    pub payload: [u8; PAYLOAD_BYTES],
}

#[cfg(feature = "packet")]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(C)]
pub struct Ack {
    pub lane: u8,
    pub producer: u8,
    pub sequence: u16,
    pub checksum: u32,
    pub sent_cycles: u32,
    pub received_cycles: u32,
    pub filtered: [i32; 3],
}

#[cfg(not(feature = "packet"))]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(C)]
pub struct Ack {
    pub lane: u8,
    pub producer: u8,
    pub sequence: u16,
    pub checksum: u32,
    pub sent_cycles: u32,
    pub received_cycles: u32,
}

const _: [(); 248] = [(); core::mem::size_of::<Event>()];
#[cfg(feature = "packet")]
const _: [(); 28] = [(); core::mem::size_of::<Ack>()];
#[cfg(not(feature = "packet"))]
const _: [(); 16] = [(); core::mem::size_of::<Ack>()];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ProtocolError {
    InvalidLane,
    InvalidProducer,
    InvalidWorker,
    WrongWorker,
    OutOfOrder,
    InvalidPayload,
    InvalidChecksum,
    PacketRejected,
    FilterMismatch,
    TooManyEvents,
    Incomplete,
    SampleCount,
}

pub const fn token(lane: u8, producer: u8, sequence: u16) -> u32 {
    (lane as u32) << 24 | (producer as u32) << 16 | sequence as u32
}

pub const fn checksum(lane: u8, producer: u8, sequence: u16) -> u32 {
    token(lane, producer, sequence) ^ CHECKSUM_SALT
}

pub const fn is_sample(sequence: u16) -> bool {
    sequence % SAMPLE_EVERY == 0
}

pub const fn micros(cycle_delta: u32) -> u32 {
    cycle_delta / CYCLES_PER_US + (cycle_delta % CYCLES_PER_US != 0) as u32
}

impl Event {
    pub fn new(lane: usize, producer: usize, sequence: u16) -> Result<Self, ProtocolError> {
        validate_endpoint(lane, producer, sequence)?;
        let lane = lane as u8;
        let producer = producer as u8;
        let mut payload = [0xa5; PAYLOAD_BYTES];
        #[cfg(feature = "packet")]
        packet_service::build(&mut payload, lane, producer, sequence);
        Ok(Self {
            lane,
            producer,
            sequence,
            sent_cycles: 0,
            checksum: checksum(lane, producer, sequence),
            payload,
        })
    }

    /// Record the producer timestamp after payload construction is complete.
    pub fn stamp(&mut self, sent_cycles: u32) {
        self.sent_cycles = if is_sample(self.sequence) {
            sent_cycles
        } else {
            0
        };
    }

    fn valid_payload(&self) -> bool {
        #[cfg(feature = "packet")]
        {
            self.checksum == checksum(self.lane, self.producer, self.sequence)
        }
        #[cfg(not(feature = "packet"))]
        {
            self.payload[0] == 0xa5
                && self.payload[PAYLOAD_BYTES - 1] == 0xa5
                && self.checksum == checksum(self.lane, self.producer, self.sequence)
        }
    }
}

fn validate_endpoint(lane: usize, producer: usize, sequence: u16) -> Result<(), ProtocolError> {
    if lane >= LANES {
        return Err(ProtocolError::InvalidLane);
    }
    if producer >= PRODUCERS {
        return Err(ProtocolError::InvalidProducer);
    }
    if sequence >= SEQUENCES {
        return Err(ProtocolError::OutOfOrder);
    }
    Ok(())
}

/// Result after application validation. The caller supplies the receive clock
/// reading only after validation and packet work have completed.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ValidatedEvent {
    ack: Ack,
}

impl ValidatedEvent {
    pub const fn acknowledge(self, received_cycles: u32) -> Ack {
        Ack {
            received_cycles: if is_sample(self.ack.sequence) {
                received_cycles
            } else {
                0
            },
            ..self.ack
        }
    }
}

pub struct WorkerState {
    id: usize,
    next: [u16; LANES_PER_WORKER * PRODUCERS],
    #[cfg(feature = "packet")]
    filters: [[i32; 3]; LANES_PER_WORKER * PRODUCERS],
}

impl WorkerState {
    pub const fn new(id: usize) -> Self {
        Self {
            id,
            next: [0; LANES_PER_WORKER * PRODUCERS],
            #[cfg(feature = "packet")]
            filters: [[0; 3]; LANES_PER_WORKER * PRODUCERS],
        }
    }

    pub fn validate(&mut self, event: &Event) -> Result<ValidatedEvent, ProtocolError> {
        if self.id >= WORKERS {
            return Err(ProtocolError::InvalidWorker);
        }
        let lane = usize::from(event.lane);
        let producer = usize::from(event.producer);
        validate_endpoint(lane, producer, event.sequence)?;
        let local_lane = lane
            .checked_sub(self.id * LANES_PER_WORKER)
            .filter(|&index| index < LANES_PER_WORKER)
            .ok_or(ProtocolError::WrongWorker)?;
        let state_index = local_lane * PRODUCERS + producer;
        if event.sequence != self.next[state_index] {
            return Err(ProtocolError::OutOfOrder);
        }
        if !event.valid_payload() {
            return Err(
                if event.checksum != checksum(event.lane, event.producer, event.sequence) {
                    ProtocolError::InvalidChecksum
                } else {
                    ProtocolError::InvalidPayload
                },
            );
        }

        #[cfg(feature = "packet")]
        let filtered = {
            let mut candidate = self.filters[state_index];
            let result = packet_service::process(
                &event.payload,
                token(event.lane, event.producer, event.sequence),
                &mut candidate,
            )
            .map_err(|_| ProtocolError::PacketRejected)?;
            self.filters[state_index] = candidate;
            result
        };

        self.next[state_index] += 1;
        let ack = Ack {
            lane: event.lane,
            producer: event.producer,
            sequence: event.sequence,
            checksum: event.checksum,
            sent_cycles: event.sent_cycles,
            received_cycles: 0,
            #[cfg(feature = "packet")]
            filtered,
        };
        Ok(ValidatedEvent { ack })
    }

    pub fn is_complete(&self) -> bool {
        self.next.iter().all(|&count| count == SEQUENCES)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Summary {
    pub received: usize,
    pub digest: u32,
    pub samples: usize,
    pub inbound_p50_us: u32,
    pub inbound_p99_us: u32,
    pub outbound_p50_us: u32,
    pub outbound_p99_us: u32,
    pub inbound_max_us: u32,
    pub outbound_max_us: u32,
}

pub struct CollectorState {
    next: [u16; LANES * PRODUCERS],
    received: usize,
    digest: u32,
    inbound_us: [u32; SAMPLES],
    outbound_us: [u32; SAMPLES],
    samples: usize,
    #[cfg(feature = "packet")]
    filters: [[i32; 3]; LANES * PRODUCERS],
}

impl CollectorState {
    pub const fn new() -> Self {
        Self {
            next: [0; LANES * PRODUCERS],
            received: 0,
            digest: 0,
            inbound_us: [0; SAMPLES],
            outbound_us: [0; SAMPLES],
            samples: 0,
            #[cfg(feature = "packet")]
            filters: [[0; 3]; LANES * PRODUCERS],
        }
    }

    pub fn accept(
        &mut self,
        worker: usize,
        ack: &Ack,
        collected_cycles: u32,
    ) -> Result<(), ProtocolError> {
        if worker >= WORKERS {
            return Err(ProtocolError::InvalidWorker);
        }
        let lane = usize::from(ack.lane);
        let producer = usize::from(ack.producer);
        validate_endpoint(lane, producer, ack.sequence)?;
        if lane / LANES_PER_WORKER != worker {
            return Err(ProtocolError::WrongWorker);
        }
        let state_index = lane * PRODUCERS + producer;
        if ack.sequence != self.next[state_index] {
            return Err(ProtocolError::OutOfOrder);
        }
        if ack.checksum != checksum(ack.lane, ack.producer, ack.sequence) {
            return Err(ProtocolError::InvalidChecksum);
        }

        #[cfg(feature = "packet")]
        {
            let mut payload = [0; PAYLOAD_BYTES];
            packet_service::build(&mut payload, ack.lane, ack.producer, ack.sequence);
            let mut candidate = self.filters[state_index];
            let expected = packet_service::process(
                &payload,
                token(ack.lane, ack.producer, ack.sequence),
                &mut candidate,
            )
            .map_err(|_| ProtocolError::PacketRejected)?;
            if ack.filtered != expected {
                return Err(ProtocolError::FilterMismatch);
            }
            self.filters[state_index] = candidate;
        }

        if self.received >= LANES * PRODUCERS * usize::from(SEQUENCES) {
            return Err(ProtocolError::TooManyEvents);
        }
        if is_sample(ack.sequence) && self.samples >= SAMPLES {
            return Err(ProtocolError::SampleCount);
        }
        self.next[state_index] += 1;
        self.received += 1;
        self.digest = self.digest.wrapping_add(ack.checksum);
        if is_sample(ack.sequence) {
            self.inbound_us[self.samples] =
                micros(ack.received_cycles.wrapping_sub(ack.sent_cycles));
            self.outbound_us[self.samples] =
                micros(collected_cycles.wrapping_sub(ack.received_cycles));
            self.samples += 1;
        }
        Ok(())
    }

    pub fn finish(&mut self) -> Result<Summary, ProtocolError> {
        let expected_events = LANES * PRODUCERS * usize::from(SEQUENCES);
        if self.received != expected_events || self.next.iter().any(|&n| n != SEQUENCES) {
            return Err(ProtocolError::Incomplete);
        }
        if self.samples != SAMPLES {
            return Err(ProtocolError::SampleCount);
        }
        self.inbound_us[..self.samples].sort_unstable();
        self.outbound_us[..self.samples].sort_unstable();
        Ok(Summary {
            received: self.received,
            digest: self.digest,
            samples: self.samples,
            inbound_p50_us: nearest_rank(&self.inbound_us[..self.samples], 50),
            inbound_p99_us: nearest_rank(&self.inbound_us[..self.samples], 99),
            outbound_p50_us: nearest_rank(&self.outbound_us[..self.samples], 50),
            outbound_p99_us: nearest_rank(&self.outbound_us[..self.samples], 99),
            inbound_max_us: self.inbound_us[self.samples - 1],
            outbound_max_us: self.outbound_us[self.samples - 1],
        })
    }
}

impl Default for CollectorState {
    fn default() -> Self {
        Self::new()
    }
}

fn nearest_rank(sorted: &[u32], percent: usize) -> u32 {
    let rank = (sorted.len() * percent).div_ceil(100).saturating_sub(1);
    sorted[rank]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fixed_topology_sizes_and_timer_conversions_match_reference() {
        assert_eq!((PRODUCERS, WORKERS, LANES, QUEUES), (4, 15, 45, 60));
        assert_eq!(LANES * PRODUCERS * usize::from(SEQUENCES), 5_760);
        assert_eq!(SAMPLES, 720);
        assert_eq!(core::mem::size_of::<Event>(), 248);
        #[cfg(feature = "packet")]
        assert_eq!(core::mem::size_of::<Ack>(), 28);
        #[cfg(not(feature = "packet"))]
        assert_eq!(core::mem::size_of::<Ack>(), 16);
        assert_eq!(micros(0), 0);
        assert_eq!(micros(240), 1);
        assert_eq!(micros(241), 2);
        assert_eq!(micros(u32::MAX.wrapping_sub(3)), 17_895_698);
    }

    #[test]
    fn rejects_bad_event_without_consuming_sequence_or_filter_state() {
        let mut worker = WorkerState::new(0);
        let mut valid = Event::new(0, 0, 0).unwrap();
        valid.stamp(100);
        let mut bad_order = valid;
        bad_order.sequence = 1;
        assert_eq!(worker.validate(&bad_order), Err(ProtocolError::OutOfOrder));
        let mut bad_lane = valid;
        bad_lane.lane = LANES as u8;
        assert_eq!(worker.validate(&bad_lane), Err(ProtocolError::InvalidLane));
        let mut bad_producer = valid;
        bad_producer.producer = PRODUCERS as u8;
        assert_eq!(
            worker.validate(&bad_producer),
            Err(ProtocolError::InvalidProducer)
        );
        let mut bad_checksum = valid;
        bad_checksum.checksum ^= 1;
        assert_eq!(
            worker.validate(&bad_checksum),
            Err(ProtocolError::InvalidChecksum)
        );
        #[cfg(feature = "packet")]
        {
            let mut bad_packet = valid;
            bad_packet.payload[4] ^= 1;
            assert_eq!(
                worker.validate(&bad_packet),
                Err(ProtocolError::PacketRejected)
            );
        }
        assert_eq!(
            worker
                .validate(&valid)
                .unwrap()
                .acknowledge(123)
                .received_cycles,
            123
        );
    }

    #[test]
    fn rejects_wrong_lane_assignment_and_ack_order() {
        let mut worker = WorkerState::new(1);
        let event = Event::new(0, 0, 0).unwrap();
        assert_eq!(worker.validate(&event), Err(ProtocolError::WrongWorker));

        let mut collector = CollectorState::new();
        let mut worker = WorkerState::new(0);
        let ack = worker
            .validate(&Event::new(0, 0, 0).unwrap())
            .unwrap()
            .acknowledge(0);
        assert_eq!(
            collector.accept(1, &ack, 0),
            Err(ProtocolError::WrongWorker)
        );
        let mut out_of_order = ack;
        out_of_order.sequence = 1;
        assert_eq!(
            collector.accept(0, &out_of_order, 0),
            Err(ProtocolError::OutOfOrder)
        );
        assert_eq!(collector.accept(0, &ack, 0), Ok(()));
    }

    #[test]
    fn complete_topology_matches_reference_digest_and_per_run_quantiles() {
        let mut collector = CollectorState::new();
        let mut workers: [WorkerState; WORKERS] = core::array::from_fn(WorkerState::new);
        for sequence in 0..SEQUENCES {
            for lane in 0..LANES {
                for producer in 0..PRODUCERS {
                    let sent = 1_000 + u32::from(sequence) * 240;
                    let received = sent.wrapping_add(481);
                    let collected = received.wrapping_add(721);
                    let mut event = Event::new(lane, producer, sequence).unwrap();
                    event.stamp(sent);
                    let worker = lane / LANES_PER_WORKER;
                    let ack = workers[worker]
                        .validate(&event)
                        .unwrap()
                        .acknowledge(received);
                    collector.accept(worker, &ack, collected).unwrap();
                }
            }
        }
        assert!(workers.iter().all(WorkerState::is_complete));
        let summary = collector.finish().unwrap();
        assert_eq!(summary.received, 5_760);
        assert_eq!(summary.digest, EXPECTED_DIGEST);
        assert_eq!(summary.samples, 720);
        assert_eq!((summary.inbound_p50_us, summary.inbound_p99_us), (3, 3));
        assert_eq!((summary.outbound_p50_us, summary.outbound_p99_us), (4, 4));
    }

    #[cfg(feature = "packet")]
    #[test]
    fn mismatched_filter_reply_does_not_advance_collector_filter_state() {
        let mut worker = WorkerState::new(0);
        let mut collector = CollectorState::new();
        let event = Event::new(0, 0, 0).unwrap();
        let mut ack = worker.validate(&event).unwrap().acknowledge(0);
        ack.filtered[0] ^= 1;
        assert_eq!(
            collector.accept(0, &ack, 0),
            Err(ProtocolError::FilterMismatch)
        );
        ack.filtered[0] ^= 1;
        assert_eq!(collector.accept(0, &ack, 0), Ok(()));
    }
}
