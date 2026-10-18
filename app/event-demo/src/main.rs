//! Event-driven architecture demonstration.
//!
//! The app knows only product services and their connections. Each active
//! service acquires and owns its required HAL capability internally.
#![forbid(unsafe_code)]

use std::time::{Duration, Instant};

use navigation::{
    FusionService, GnssService, HealthService, ImuService,
};

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
    let imu = imu.start()?;
    let gnss = gnss.start()?;

    println!("EVENT_DEMO topology:");
    println!("  ImuService  owns HAL resource + processing + thread/event loop");
    println!("  GnssService owns HAL resource + processing + thread/event loop");
    println!("  FusionService owns one bounded inbox + fusion state + owner thread");
    println!("  active services use one wait point and run each event to completion");
    println!("  app has no provider dependency; build selects providers inside each HAL capability");
    println!("  app orchestration = create -> connect -> start -> command -> stop");

    let started = Instant::now();
    let deadline = started + duration;
    let total_ms = u64::try_from(duration.as_millis()).unwrap_or(u64::MAX);
    let phase1_at = started + Duration::from_millis((total_ms / 3).max(1));
    let phase2_at =
        started + Duration::from_millis((total_ms.saturating_mul(2) / 3).max(2));

    let mut phase1_done = false;
    let mut phase2_done = false;
    let mut health = HealthService::default();
    let mut observed = 0u64;

    while Instant::now() < deadline {
        let now = Instant::now();

        if !phase1_done && now >= phase1_at {
            imu.set_period(Duration::from_millis(10))?;
            gnss.pause()?;
            let imu_status = imu.status()?;
            let gnss_status = gnss.status()?;
            println!(
                "CONTROL phase=1 imu_period_ms={} imu_sampling={} gnss_sampling={} imu_produced={} gnss_produced={}",
                imu_status.period.as_millis(),
                imu_status.sampling,
                gnss_status.sampling,
                imu_status.stats.produced,
                gnss_status.stats.produced,
            );
            phase1_done = true;
        }

        if !phase2_done && now >= phase2_at {
            gnss.set_period(Duration::from_millis(100))?;
            gnss.resume()?;
            let imu_status = imu.status()?;
            let gnss_status = gnss.status()?;
            println!(
                "CONTROL phase=2 imu_period_ms={} gnss_period_ms={} gnss_sampling={} imu_produced={} gnss_produced={}",
                imu_status.period.as_millis(),
                gnss_status.period.as_millis(),
                gnss_status.sampling,
                imu_status.stats.produced,
                gnss_status.stats.produced,
            );
            phase2_done = true;
        }

        let remaining = deadline.saturating_duration_since(Instant::now());
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
