//! Multi-instance proof using two independent service-owned IMU resources.
//!
//! The app depends only on reusable navigation services. Each ImuService calls
//! the capability-local HAL acquisition path independently at start().
#![forbid(unsafe_code)]

use std::io;
use std::time::{Duration, Instant};

use navigation::{
    FusionConfig, FusionHandle, FusionService, ImuConfig, ImuHandle, ImuService,
};

fn first_state(fusion: &FusionHandle, name: &str) -> io::Result<u64> {
    fusion
        .recv_timeout(Duration::from_secs(1))
        .map(|state| state.imu_sequence)
        .map_err(|error| io::Error::new(
            io::ErrorKind::TimedOut,
            format!("{name} did not publish an IMU state: {error}"),
        ))
}

fn wait_for_progress(
    imu: &ImuHandle,
    fusion: &FusionHandle,
    baseline: u64,
    name: &str,
) -> io::Result<u64> {
    let deadline = Instant::now() + Duration::from_secs(1);
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                format!("{name} did not make progress"),
            ));
        }

        match fusion.recv_timeout(remaining.min(Duration::from_millis(50))) {
            Ok(_) | Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                return Err(io::Error::new(
                    io::ErrorKind::BrokenPipe,
                    format!("{name} fusion output disconnected"),
                ));
            }
        }

        let status = imu.status()?;
        if status.stats.produced > baseline {
            return Ok(status.stats.produced);
        }
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let fusion_config = FusionConfig {
        inbox_capacity: 8,
        output_capacity: 8,
        publish_every_imu: 1,
    };

    let (fusion_a, inputs_a) = FusionService::with_config(fusion_config);
    let (fusion_b, inputs_b) = FusionService::with_config(fusion_config);

    let imu_a = ImuService::new(inputs_a.imu).with_config(ImuConfig {
        period: Duration::from_millis(10),
    });
    let imu_b = ImuService::new(inputs_b.imu).with_config(ImuConfig {
        period: Duration::from_millis(15),
    });

    let fusion_a = fusion_a.start()?;
    let fusion_b = fusion_b.start()?;
    let imu_a = imu_a.start()?;
    let imu_b = imu_b.start()?;

    println!("DUAL_IMU topology:");
    println!("  app owns two independent FusionService instances");
    println!("  app owns two independent ImuService instances");
    println!("  each ImuService acquires its own HAL resource at start()");
    println!("  no app/provider dependency, registry, instance ID, or global HAL object");

    let first_a = first_state(&fusion_a, "pipeline A")?;
    let first_b = first_state(&fusion_b, "pipeline B")?;
    if first_a != 1 || first_b != 1 {
        return Err(format!(
            "HAL instances are not independent: first sequences were ({first_a}, {first_b})"
        )
        .into());
    }

    imu_a.pause()?;
    let paused_a = imu_a.status()?;
    let b_before = imu_b.status()?;
    if paused_a.sampling || !b_before.sampling {
        return Err("pause leaked across IMU service instances".into());
    }

    let b_after =
        wait_for_progress(&imu_b, &fusion_b, b_before.stats.produced, "pipeline B")?;
    let still_paused_a = imu_a.status()?;
    if still_paused_a.stats.produced != paused_a.stats.produced {
        return Err("paused IMU instance continued producing".into());
    }

    imu_a.resume()?;
    let resumed_a =
        wait_for_progress(&imu_a, &fusion_a, still_paused_a.stats.produced, "pipeline A")?;

    let stats_a = imu_a.stop()?;
    let stats_b = imu_b.stop()?;
    let fusion_stats_a = fusion_a.stop()?;
    let fusion_stats_b = fusion_b.stop()?;

    if stats_a.produced == 0
        || stats_b.produced == 0
        || fusion_stats_a.inputs == 0
        || fusion_stats_b.inputs == 0
    {
        return Err("multi-instance proof did not exercise both pipelines".into());
    }

    println!(
        "DUAL_IMU PASS first=({first_a},{first_b}) paused_a={} b_progress={}->{} resumed_a={} final=({},{})",
        paused_a.stats.produced,
        b_before.stats.produced,
        b_after,
        resumed_a,
        stats_a.produced,
        stats_b.produced,
    );
    Ok(())
}

fn main() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("dual-imu-demo: {error}");
            std::process::ExitCode::FAILURE
        }
    }
}
