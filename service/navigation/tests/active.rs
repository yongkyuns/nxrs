use imu::Device;
use std::time::Duration;

use navigation::{FusionConfig, FusionService, GnssService, HealthService, ImuService};

// These integration cases use the profile's same canonical primary device.
// Hold the fixture through joined service shutdown, not just startup.
static PRIMARY: std::sync::Mutex<()> = std::sync::Mutex::new(());

fn start_services() -> (
    navigation::FusionHandle,
    navigation::ImuHandle,
    navigation::GnssHandle,
) {
    let (fusion, inputs) = FusionService::with_config(FusionConfig {
        inbox_capacity: 8,
        output_capacity: 4,
        publish_every_imu: 2,
    });

    let imu = ImuService::new(inputs.imu);
    let gnss = GnssService::new(inputs.gnss);

    (
        fusion.start().unwrap(),
        imu.start_on(imu_devices::primary_imu()).unwrap(),
        gnss.start_on(gnss_devices::primary_gnss()).unwrap(),
    )
}

#[test]
fn active_services_own_hal_threads_and_lifecycle() {
    let _primary = PRIMARY.lock().unwrap();
    let (fusion, imu, gnss) = start_services();
    assert_eq!(
        imu_devices::primary_imu().open_default(|_| Ok(())).err(),
        Some(imu::DeviceError::Busy)
    );
    let (other_fusion, other_inputs) = FusionService::new();
    let other_fusion = other_fusion.start().unwrap();
    let other_imu = ImuService::new(other_inputs.imu)
        .start_on(imu_devices::secondary())
        .unwrap();

    let mut health = HealthService::default();
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    let state = loop {
        let state = fusion
            .recv_timeout(deadline.saturating_duration_since(std::time::Instant::now()))
            .unwrap();
        if state.imu_sequence != 0 && state.gnss_sequence != 0 {
            break state;
        }
        assert!(std::time::Instant::now() < deadline);
    };
    let snapshot = health.observe(state);
    assert!(snapshot.imu_alive && snapshot.gnss_alive);

    assert!(
        other_fusion
            .recv_timeout(Duration::from_secs(2))
            .unwrap()
            .imu_sequence
            > 0
    );
    assert!(other_imu.stop().unwrap().produced > 0);
    other_fusion.stop().unwrap();

    let imu_stats = imu.stop().unwrap();
    let gnss_stats = gnss.stop().unwrap();
    let fusion_stats = fusion.stop().unwrap();

    assert!(imu_stats.produced > 0);
    assert!(gnss_stats.produced > 0);
    assert!(fusion_stats.inputs >= 2);

    // A completed service shutdown releases the same device for its next owner.
    use imu::Session;
    imu_devices::primary_imu()
        .open_default(|_| Ok(()))
        .unwrap()
        .join()
        .unwrap();
}

#[test]
fn sensor_commands_are_processed_inside_the_service_loops() {
    let _primary = PRIMARY.lock().unwrap();
    let (fusion, imu, gnss) = start_services();

    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    while gnss.status().unwrap().stats.produced == 0 || imu.status().unwrap().stats.produced == 0 {
        assert!(std::time::Instant::now() < deadline);
        let _ = fusion.recv_timeout(Duration::from_millis(50));
    }
    let before = imu.status().unwrap();
    assert!(before.sampling);
    assert!(before.stats.produced > 0);

    imu.pause().unwrap();
    let paused = imu.status().unwrap();
    assert!(!paused.sampling);
    let paused_count = paused.stats.produced;
    std::thread::sleep(Duration::from_millis(25));
    assert_eq!(imu.status().unwrap().stats.produced, paused_count);

    imu.resume().unwrap();
    let resumed = imu.status().unwrap();
    assert!(resumed.sampling);
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    while imu.status().unwrap().stats.produced == paused_count {
        assert!(std::time::Instant::now() < deadline);
        let _ = fusion.recv_timeout(Duration::from_millis(50));
    }

    let gnss_before = gnss.status().unwrap();
    gnss.pause().unwrap();
    let gnss_paused = gnss.status().unwrap();
    assert!(!gnss_paused.sampling);
    assert!(gnss_paused.stats.produced >= gnss_before.stats.produced);
    gnss.resume().unwrap();
    let gnss_resumed = gnss.status().unwrap();
    assert!(gnss_resumed.sampling);

    let imu_stats = imu.stop().unwrap();
    let gnss_stats = gnss.stop().unwrap();
    let fusion_stats = fusion.stop().unwrap();
    assert!(imu_stats.produced > 0);
    assert!(gnss_stats.produced > 0);
    assert!(fusion_stats.inputs > 0);
}
