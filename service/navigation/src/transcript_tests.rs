use std::sync::mpsc::{self, Receiver, SyncSender};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use ::gnss as gnss_hal;
use ::imu as imu_hal;
use nxrs_simulation::{
    Event as SourceEvent, EventKey, GnssObservation, ImuObservation, Observation, Simulation,
    SimulationSpec, Source, DEFAULT_GNSS_PERIOD_US, DEFAULT_IMU_PERIOD_US,
};

use crate::gnss::{GnssService, TestInput as GnssTestInput};
use crate::imu::{ImuService, TestInput as ImuTestInput};
use crate::{FusionConfig, FusionService, NavState};

const RUN_DURATION_US: u64 = 2_000_000;
// Independent fixture deadlines bracket the 10 ms source tick and each occurs once.
const PAUSE_AT_US: u64 = 670_000;
const RESUME_AT_US: u64 = 1_340_000;
const WAIT: Duration = Duration::from_secs(2);

#[derive(Clone, Copy)]
enum WakeOrder {
    ImuFirst,
    GnssFirst,
}

struct Producer {
    source: Source,
    request: SyncSender<()>,
    reply: Receiver<Option<SourceEvent>>,
    join: JoinHandle<u64>,
}

impl Producer {
    fn start(source: Source, endpoint: nxrs_simulation::EndpointLease) -> Self {
        let (request, requests) = mpsc::sync_channel(1);
        let (reply, replies) = mpsc::sync_channel(1);
        let join = thread::Builder::new()
            .name(
                match source {
                    Source::Imu => "simulation-imu-test-producer",
                    Source::Gnss => "simulation-gnss-test-producer",
                }
                .into(),
            )
            .spawn(move || {
                while requests.recv().is_ok() {
                    let event = endpoint.try_recv().unwrap();
                    assert!(
                        endpoint.try_recv().unwrap().is_none(),
                        "multiple events per source tick"
                    );
                    reply.send(event).unwrap();
                }
                endpoint.dropped()
            })
            .unwrap();
        Self {
            source,
            request,
            reply: replies,
            join,
        }
    }

    fn drain(&self) -> Option<SourceEvent> {
        self.request
            .try_send(())
            .expect("producer request channel full");
        self.reply
            .recv_timeout(WAIT)
            .expect("producer drain timed out")
    }

    fn stop(self) -> u64 {
        drop(self.request);
        self.join.join().unwrap()
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
enum TranscriptRecord {
    Control {
        time_us: u64,
        sampling: bool,
        produced: u64,
        dropped: u64,
    },
    Navigation {
        key: EventKey,
        state: NavState,
    },
    Suppressed {
        key: EventKey,
    },
}

struct Trial {
    transcript: Vec<TranscriptRecord>,
    tie_wake_orders: Vec<[Source; 2]>,
    stats: (u64, u64, u64, u64),
}

#[derive(Default)]
struct Counts {
    generated: [usize; 2],
    imu: u64,
    gnss: u64,
    suppressed_gnss: u64,
}

struct IdleImu;

impl imu_hal::Imu for IdleImu {
    fn request_stop(&mut self) -> Result<(), imu_hal::DeviceError> {
        Ok(())
    }
    fn join(&mut self) -> Result<(), imu_hal::DeviceError> {
        Ok(())
    }
    fn is_finished(&self) -> bool {
        true
    }
    fn dropped(&self) -> u32 {
        0
    }
}

struct IdleGnss;

impl gnss_hal::Gnss for IdleGnss {
    fn request_stop(&mut self) -> Result<(), gnss_hal::DeviceError> {
        Ok(())
    }
    fn join(&mut self) -> Result<(), gnss_hal::DeviceError> {
        Ok(())
    }
    fn is_finished(&self) -> bool {
        true
    }
    fn dropped(&self) -> u32 {
        0
    }
}

fn imu_sample(key: EventKey, observation: ImuObservation) -> imu_hal::ImuSample {
    imu_hal::ImuSample {
        sequence: key.sequence.saturating_add(1),
        timestamp_ms: key.time_us / 1_000,
        sample_interval_us: DEFAULT_IMU_PERIOD_US as u32,
        accel_mps2: observation
            .accel_ug
            .map(|value| value as f32 * (9.80665 / 1_000_000.0)),
        gyro_rps: observation
            .gyro_udps
            .map(|value| (value as f32 / 1_000_000.0).to_radians()),
    }
}

fn gnss_fix(key: EventKey, observation: GnssObservation) -> gnss_hal::GnssFix {
    gnss_hal::GnssFix {
        sequence: key.sequence.saturating_add(1),
        timestamp_ms: key.time_us / 1_000,
        latitude_deg: f64::from(observation.latitude_e7) / 10_000_000.0,
        longitude_deg: f64::from(observation.longitude_e7) / 10_000_000.0,
        speed_mps: observation.speed_mm_s as f32 / 1_000.0,
        course_rad: observation.course_mrad.map(|value| value as f32 / 1_000.0),
    }
}

fn admit(event: SourceEvent, imu: &ImuTestInput, gnss: &GnssTestInput) -> Result<(), ()> {
    match (event.key.source, event.observation) {
        (Source::Imu, Observation::Imu(value)) => imu.sample(imu_sample(event.key, value)),
        (Source::Gnss, Observation::Gnss(value)) => gnss.fix(gnss_fix(event.key, value)),
        _ => Err(()),
    }
}

fn record_control(
    time_us: u64,
    pause: bool,
    gnss: &crate::GnssHandle,
    counts: &Counts,
    transcript: &mut Vec<TranscriptRecord>,
) {
    if pause {
        gnss.pause().unwrap();
    } else {
        gnss.resume().unwrap();
    }
    let status = gnss.status().unwrap();
    assert_eq!(status.sampling, !pause);
    assert_eq!(status.stats.produced, counts.gnss);
    assert_eq!(status.stats.dropped, counts.suppressed_gnss);
    transcript.push(TranscriptRecord::Control {
        time_us,
        sampling: status.sampling,
        produced: status.stats.produced,
        dropped: status.stats.dropped,
    });
}

fn process_event(
    event: SourceEvent,
    imu: &crate::ImuHandle,
    gnss: &crate::GnssHandle,
    fusion: &crate::FusionHandle,
    counts: &mut Counts,
    transcript: &mut Vec<TranscriptRecord>,
) {
    let forwarded = match event.key.source {
        Source::Imu => {
            counts.generated[0] += 1;
            counts.imu += 1;
            let status = imu.status().unwrap();
            assert!(status.sampling);
            assert_eq!(status.stats.produced, counts.imu);
            assert_eq!(status.stats.errors, 0);
            true
        }
        Source::Gnss => {
            counts.generated[1] += 1;
            let status = gnss.status().unwrap();
            assert_eq!(status.stats.errors, 0);
            if status.sampling {
                counts.gnss += 1;
                assert_eq!(status.stats.produced, counts.gnss);
                assert_eq!(status.stats.dropped, counts.suppressed_gnss);
                true
            } else {
                counts.suppressed_gnss += 1;
                assert_eq!(status.stats.produced, counts.gnss);
                assert_eq!(status.stats.dropped, counts.suppressed_gnss);
                transcript.push(TranscriptRecord::Suppressed { key: event.key });
                false
            }
        }
    };
    if forwarded {
        let state = fusion.recv_timeout(WAIT).unwrap();
        let sequence = event.key.sequence.saturating_add(1);
        match event.key.source {
            Source::Imu => assert_eq!(state.imu_sequence, sequence),
            Source::Gnss => assert_eq!(state.gnss_sequence, sequence),
        }
        transcript.push(TranscriptRecord::Navigation {
            key: event.key,
            state,
        });
    }
}

fn run_trial(order: WakeOrder) -> Trial {
    let simulation = Simulation::new(SimulationSpec::coordinated(0x5eed)).unwrap();
    let imu_endpoint = simulation.lease(Source::Imu).unwrap();
    let gnss_endpoint = simulation.lease(Source::Gnss).unwrap();
    simulation.start().unwrap();

    let (fusion, outputs) = FusionService::with_config(FusionConfig {
        inbox_capacity: 8,
        output_capacity: 8,
        publish_every_imu: 1,
    });
    let fusion = fusion.start().unwrap();
    let (imu, imu_input) = ImuService::new(outputs.imu)
        .start_with_test_device(IdleImu)
        .unwrap();
    let (gnss, gnss_input) = GnssService::new(outputs.gnss)
        .start_with_test_device(IdleGnss)
        .unwrap();
    let producers = [
        Producer::start(Source::Imu, imu_endpoint),
        Producer::start(Source::Gnss, gnss_endpoint),
    ];

    let mut transcript = Vec::new();
    let mut counts = Counts::default();
    let mut tie_wake_orders = Vec::new();

    for target_us in
        (DEFAULT_IMU_PERIOD_US..=RUN_DURATION_US).step_by(DEFAULT_IMU_PERIOD_US as usize)
    {
        if target_us == PAUSE_AT_US {
            record_control(PAUSE_AT_US, true, &gnss, &counts, &mut transcript);
        }
        if target_us == RESUME_AT_US {
            record_control(RESUME_AT_US, false, &gnss, &counts, &mut transcript);
        }

        let report = simulation.advance_to(target_us).unwrap();
        assert_eq!(report.dropped, 0, "simulation source queue lost events");
        let wake_order = match order {
            WakeOrder::ImuFirst => [Source::Imu, Source::Gnss],
            WakeOrder::GnssFirst => [Source::Gnss, Source::Imu],
        };
        let mut events = Vec::new();
        for source in wake_order {
            if let Some(event) = producers
                .iter()
                .find(|worker| worker.source == source)
                .unwrap()
                .drain()
            {
                events.push(event);
            }
        }
        events.sort_by_key(|event| event.key);
        assert_eq!(
            report.generated,
            events.len(),
            "generated source events incomplete"
        );
        if events.len() == 2 {
            tie_wake_orders.push(wake_order);
        }

        for event in events {
            admit(event, &imu_input, &gnss_input)
                .expect("service device-message inbox full, closed, or mismatched");
            process_event(event, &imu, &gnss, &fusion, &mut counts, &mut transcript);
        }
    }

    assert_eq!(simulation.now_us(), RUN_DURATION_US);
    assert_eq!(
        transcript
            .iter()
            .filter(|record| matches!(record, TranscriptRecord::Control { .. }))
            .count(),
        2
    );
    assert_eq!(
        counts.generated[0],
        (RUN_DURATION_US / DEFAULT_IMU_PERIOD_US) as usize
    );
    assert_eq!(
        counts.generated[1],
        (RUN_DURATION_US / DEFAULT_GNSS_PERIOD_US) as usize
    );
    assert_eq!(tie_wake_orders.len(), 10);
    assert_eq!(counts.imu, counts.generated[0] as u64);
    assert_eq!(
        counts.gnss + counts.suppressed_gnss,
        counts.generated[1] as u64
    );
    assert_eq!(counts.suppressed_gnss, 3);

    let source_drops: u64 = producers.into_iter().map(Producer::stop).sum();
    assert_eq!(source_drops, 0);
    let imu_stats = imu.stop().unwrap();
    let gnss_stats = gnss.stop().unwrap();
    let fusion_stats = fusion.stop().unwrap();
    assert_eq!(
        (imu_stats.produced, imu_stats.dropped, imu_stats.errors),
        (200, 0, 0)
    );
    assert_eq!(
        (gnss_stats.produced, gnss_stats.dropped, gnss_stats.errors),
        (7, 3, 0)
    );
    assert_eq!(
        (
            fusion_stats.inputs,
            fusion_stats.outputs,
            fusion_stats.dropped_outputs
        ),
        (207, 207, 0)
    );
    assert_eq!(
        transcript
            .iter()
            .filter(|record| matches!(record, TranscriptRecord::Navigation { .. }))
            .count(),
        207
    );

    simulation.reset_after_join().unwrap();
    Trial {
        transcript,
        tie_wake_orders,
        stats: (
            imu_stats.produced,
            gnss_stats.produced,
            gnss_stats.dropped,
            fusion_stats.outputs,
        ),
    }
}

#[test]
fn seeded_service_transcript_is_equal_under_reversed_controlled_source_wakes() {
    let imu_first = run_trial(WakeOrder::ImuFirst);
    let gnss_first = run_trial(WakeOrder::GnssFirst);
    assert_eq!(imu_first.transcript, gnss_first.transcript);
    assert_eq!(imu_first.stats, gnss_first.stats);
    assert_eq!(imu_first.stats, (200, 7, 3, 207));
    assert!(imu_first
        .tie_wake_orders
        .iter()
        .all(|order| *order == [Source::Imu, Source::Gnss]));
    assert!(gnss_first
        .tie_wake_orders
        .iter()
        .all(|order| *order == [Source::Gnss, Source::Imu]));
}
