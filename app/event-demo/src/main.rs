//! Event-driven architecture demonstration.
//!
//! The app connects portable services and selects the named IMU device.
//! Services acquire and own their HAL sessions.
#![forbid(unsafe_code)]

use std::time::{Duration, Instant};

use navigation::{FusionService, GnssService, HealthService, ImuService};

mod orchestration;

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

    let fusion = fusion.start()?;
    let imu = imu.start_on(devices::primary_imu())?;
    let gnss = gnss.start()?;

    println!("EVENT_DEMO topology:");
    println!("  ImuService  owns HAL session + processing/event loop; HAL owns acquisition");
    println!("  GnssService owns HAL session + processing/event loop; HAL owns acquisition");
    println!("  FusionService owns one bounded inbox + fusion state + owner thread");
    println!("  active services use one wait point and run each event to completion");
    println!(
        "  app selects a portable device name; the profile supplies its concrete construction"
    );
    println!("  app orchestration = create -> connect -> start -> command -> stop");

    let started = Instant::now();
    let mut schedule = orchestration::Schedule::new(duration);
    let mut phase1_done = false;
    let mut phase2_done = false;
    let mut health = HealthService::default();
    let mut observed = 0u64;

    'observe: loop {
        let elapsed = started.elapsed();
        while let Some(decision) = schedule.advance(elapsed) {
            match decision {
                orchestration::Decision::PauseGnss => {
                    gnss.pause()?;
                    let imu_status = imu.status()?;
                    let gnss_status = gnss.status()?;
                    println!(
                        "CONTROL phase=1 imu_sampling={} gnss_sampling={} imu_produced={} gnss_produced={}",
                        imu_status.sampling,
                        gnss_status.sampling,
                        imu_status.stats.produced,
                        gnss_status.stats.produced,
                    );
                    phase1_done = true;
                }
                orchestration::Decision::ResumeGnss => {
                    gnss.resume()?;
                    let imu_status = imu.status()?;
                    let gnss_status = gnss.status()?;
                    println!(
                        "CONTROL phase=2 gnss_sampling={} imu_produced={} gnss_produced={}",
                        gnss_status.sampling, imu_status.stats.produced, gnss_status.stats.produced,
                    );
                    phase2_done = true;
                }
                orchestration::Decision::Stop => break 'observe,
            }
        }

        let remaining = duration.saturating_sub(started.elapsed());
        let wait = remaining.min(Duration::from_millis(50));
        match fusion.recv_timeout(wait) {
            Ok(state) => {
                observed = observed.saturating_add(1);
                let status = health.observe(state);
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
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
        }
    }

    let imu_stats = imu.stop()?;
    let gnss_stats = gnss.stop()?;

    while let Ok(state) = fusion.try_recv() {
        health.observe(state);
        observed = observed.saturating_add(1);
    }

    let fusion_stats = fusion.stop()?;

    println!(
        "EVENT_DEMO PASS observed={} phases=({}, {}) imu={{produced:{},dropped:{},errors:{}}} gnss={{produced:{},dropped:{},errors:{}}} fusion={{inputs:{},outputs:{},dropped:{}}}",
        observed,
        phase1_done,
        phase2_done,
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

    if !phase1_done
        || !phase2_done
        || imu_stats.produced == 0
        || gnss_stats.produced == 0
        || fusion_stats.inputs == 0
        || observed == 0
    {
        return Err("demo did not exercise commands and every active service".into());
    }

    Ok(())
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
