//! Event-driven architecture demonstration.
//!
//! The app connects portable services and selects named IMU and GNSS devices.
//! Services acquire and own their HAL sessions.
#![forbid(unsafe_code)]

use std::io;
use std::sync::mpsc::TryRecvError;
use std::thread;
use std::time::{Duration, Instant};

use navigation::{
    ControlCompletion, ControlRequest, FusionHandle, FusionService, GnssHandle, GnssService,
    GnssStatus, HealthService, ImuHandle, ImuService, ImuStatus, RequestError, RequestId,
};

mod orchestration;

// Demonstration policy: retries and command expiry share the observation wait.
// These are not qualified product response-time bounds.
const CONTROL_TIMEOUT: Duration = Duration::from_secs(1);
const ADMISSION_RETRY: Duration = Duration::from_millis(5);
const OUTPUTS_PER_TURN: usize = 8;

struct PendingPhase {
    request: ControlRequest,
    imu_id: Option<RequestId>,
    gnss_id: Option<RequestId>,
    imu: Option<ImuStatus>,
    gnss: Option<GnssStatus>,
    expires: Duration,
    retry_at: Duration,
}

impl PendingPhase {
    fn new(request: ControlRequest, elapsed: Duration) -> Self {
        Self {
            request,
            imu_id: None,
            gnss_id: None,
            imu: None,
            gnss: None,
            expires: elapsed.saturating_add(CONTROL_TIMEOUT),
            retry_at: elapsed,
        }
    }

    fn advance(
        &mut self,
        elapsed: Duration,
        imu: &mut ImuHandle,
        gnss: &mut GnssHandle,
    ) -> io::Result<Option<(ImuStatus, GnssStatus)>> {
        // Expiry ends this demo's observation. It does not release a service's
        // outstanding request or claim that native acquisition has stopped.
        if elapsed >= self.expires {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "navigation control completion expired",
            ));
        }
        if elapsed >= self.retry_at {
            if self.imu_id.is_none() {
                self.imu_id = admitted(imu.try_request(ControlRequest::Status))?;
            }
            if self.gnss_id.is_none() {
                self.gnss_id = admitted(gnss.try_request(self.request))?;
            }
            self.retry_at = elapsed.saturating_add(ADMISSION_RETRY);
        }
        if let Some(completion) = imu.try_completion() {
            self.imu = Some(matched(completion, self.imu_id, ControlRequest::Status)?);
        }
        if let Some(completion) = gnss.try_completion() {
            self.gnss = Some(matched(completion, self.gnss_id, self.request)?);
        }
        Ok(self.imu.zip(self.gnss))
    }

    fn next_deadline(&self) -> Duration {
        if self.imu_id.is_none() || self.gnss_id.is_none() {
            self.expires.min(self.retry_at)
        } else {
            self.expires
        }
    }
}

fn admitted(result: Result<RequestId, RequestError>) -> io::Result<Option<RequestId>> {
    match result {
        Ok(id) => Ok(Some(id)),
        // Retain the request and attempt admission on the next bounded retry.
        Err(RequestError::Full) => Ok(None),
        Err(error) => Err(io::Error::other(format!("navigation control: {error:?}"))),
    }
}

fn matched<S>(
    completion: ControlCompletion<S>,
    expected: Option<RequestId>,
    request: ControlRequest,
) -> io::Result<S> {
    if Some(completion.id) != expected || completion.request != request {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "unexpected navigation control completion",
        ));
    }
    completion
        .result
        .map_err(|error| io::Error::other(format!("navigation control: {error:?}")))
}

#[derive(Default)]
struct Observation {
    phase1_done: bool,
    phase2_done: bool,
    observed: u64,
    health: HealthService,
}

fn duration_from_args() -> Result<Duration, Box<dyn std::error::Error>> {
    let mut args = std::env::args().skip(1);
    let Some(first) = args.next() else {
        return Ok(Duration::from_millis(2_000));
    };
    if first != "--duration-ms" {
        return Err("usage: event-demo [--duration-ms <milliseconds>]".into());
    }
    let value: u64 = args.next().ok_or("missing duration")?.parse()?;
    if args.next().is_some() || value == 0 {
        return Err("duration must be one positive integer".into());
    }
    Ok(Duration::from_millis(value))
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let duration = duration_from_args()?;

    let (fusion, inputs) = FusionService::new();
    let imu = ImuService::new(inputs.imu);
    let gnss = GnssService::new(inputs.gnss);

    let owner = thread::current();
    let wake = owner.clone();
    let fusion = fusion.start_with_wake(move || wake.unpark())?;
    let wake = owner.clone();
    let mut imu =
        match imu.start_on_with_wake(composition::imu::primary_imu(), move || wake.unpark()) {
            Ok(imu) => imu,
            Err(error) => {
                // Roll back the owner that was already started.
                if let Err(cleanup) = fusion.stop() {
                    eprintln!("event-demo: fusion rollback failed: {cleanup}");
                }
                return Err(error.into());
            }
        };
    let mut gnss =
        match gnss.start_on_with_wake(composition::gnss::primary_gnss(), move || owner.unpark()) {
            Ok(gnss) => gnss,
            Err(error) => {
                let imu_cleanup = imu.stop();
                let fusion_cleanup = fusion.stop();
                if let Err(cleanup) = imu_cleanup {
                    eprintln!("event-demo: IMU rollback failed: {cleanup}");
                }
                if let Err(cleanup) = fusion_cleanup {
                    eprintln!("event-demo: fusion rollback failed: {cleanup}");
                }
                return Err(error.into());
            }
        };

    println!("EVENT_DEMO topology:");
    println!("  ImuService  owns HAL session + processing/event loop; HAL owns acquisition");
    println!("  GnssService owns HAL session + processing/event loop; HAL owns acquisition");
    println!("  FusionService owns one bounded inbox + fusion state + owner thread");
    println!("  active services use one wait point and run each event to completion");
    println!("  app receives retained control completions and fusion output at one wait point");
    println!(
        "  app selects a portable device name; the profile supplies its concrete construction"
    );
    println!("  app orchestration = create -> connect -> start -> command -> stop");

    let mut observation = Observation::default();
    let outcome = observe(duration, &fusion, &mut imu, &mut gnss, &mut observation);

    // Join all started owners even after expiry/failure. An unconsumed control
    // completion cannot block a producer, so these joins need no reply draining.
    let imu_result = imu.stop();
    let gnss_result = gnss.stop();
    while let Ok(state) = fusion.try_recv() {
        observation.health.observe(state);
        observation.observed = observation.observed.saturating_add(1);
    }
    let fusion_result = fusion.stop();
    outcome?;
    let imu_stats = imu_result?;
    let gnss_stats = gnss_result?;
    let fusion_stats = fusion_result?;

    if !observation.phase1_done
        || !observation.phase2_done
        || imu_stats.produced == 0
        || gnss_stats.produced == 0
        || fusion_stats.inputs == 0
        || observation.observed == 0
    {
        return Err("demo did not exercise commands and every active service".into());
    }

    println!(
        "EVENT_DEMO PASS observed={} phases=({}, {}) imu={{produced:{},dropped:{},errors:{}}} gnss={{produced:{},dropped:{},errors:{}}} fusion={{inputs:{},outputs:{},dropped:{}}}",
        observation.observed,
        observation.phase1_done,
        observation.phase2_done,
        imu_stats.produced,
        imu_stats.dropped,
        imu_stats.errors,
        gnss_stats.produced,
        gnss_stats.dropped,
        gnss_stats.errors,
        fusion_stats.inputs,
        fusion_stats.outputs,
        fusion_stats.dropped_outputs,
    );

    Ok(())
}

fn observe(
    duration: Duration,
    fusion: &FusionHandle,
    imu: &mut ImuHandle,
    gnss: &mut GnssHandle,
    observation: &mut Observation,
) -> io::Result<()> {
    let started = Instant::now();
    let mut schedule = orchestration::Schedule::new(duration);
    let mut pending: Option<PendingPhase> = None;

    loop {
        // Check absolute deadlines even while an input stays continuously ready.
        let elapsed = started.elapsed();
        if elapsed >= duration {
            return Ok(());
        }
        if pending.is_none() {
            match schedule.advance(elapsed) {
                Some(orchestration::Decision::PauseGnss) => {
                    pending = Some(PendingPhase::new(ControlRequest::Pause, elapsed));
                }
                Some(orchestration::Decision::ResumeGnss) => {
                    pending = Some(PendingPhase::new(ControlRequest::Resume, elapsed));
                }
                Some(orchestration::Decision::Stop) => return Ok(()),
                None => {}
            }
        }
        if let Some(phase) = &mut pending {
            if let Some((imu_status, gnss_status)) = phase.advance(elapsed, imu, gnss)? {
                if phase.request == ControlRequest::Pause {
                    println!(
                        "CONTROL phase=1 imu_sampling={} gnss_sampling={} imu_produced={} gnss_produced={}",
                        imu_status.sampling, gnss_status.sampling,
                        imu_status.stats.produced, gnss_status.stats.produced,
                    );
                    observation.phase1_done = true;
                } else {
                    println!(
                        "CONTROL phase=2 gnss_sampling={} imu_produced={} gnss_produced={}",
                        gnss_status.sampling, imu_status.stats.produced, gnss_status.stats.produced,
                    );
                    observation.phase2_done = true;
                }
                pending = None;
                // A second scheduled phase may already be due. Reconcile it
                // before deciding whether sleeping is appropriate.
                continue;
            }
        }

        let mut drained = 0;
        while drained < OUTPUTS_PER_TURN {
            match fusion.try_recv() {
                Ok(state) => {
                    drained += 1;
                    observation.observed = observation.observed.saturating_add(1);
                    let status = observation.health.observe(state);
                    println!(
                        "NAV t={:4}ms imu={:3} gnss={:2} pos=({:5.2},{:5.2}) speed={:4.2} heading={:5.3} health=({},{})",
                        state.timestamp_ms,
                        state.imu_sequence,
                        state.gnss_sequence,
                        state.north_m,
                        state.east_m,
                        state.speed_mps,
                        state.heading_rad,
                        status.imu_alive,
                        status.gnss_alive,
                    );
                }
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => {
                    return Err(io::Error::new(
                        io::ErrorKind::BrokenPipe,
                        "fusion owner stopped",
                    ));
                }
            }
        }
        if drained == OUTPUTS_PER_TURN {
            // A coalesced wake may cover more than one batch. Never wait for a
            // new producer edge while retained output may still be actionable.
            continue;
        }
        let deadline = match &pending {
            Some(phase) => phase.next_deadline().min(duration),
            None => schedule.next_deadline(),
        };
        let remaining = deadline.saturating_sub(started.elapsed());
        if !remaining.is_zero() {
            // All three owners commit retained state before unparking this
            // thread. A publication between inspection and park retains a token.
            // No other operation in this loop consumes that park token.
            thread::park_timeout(remaining);
        }
    }
}

fn main() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("event-demo: {error}");
            std::process::ExitCode::FAILURE
        }
    }
}
