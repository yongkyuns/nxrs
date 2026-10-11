use std::time::Duration;

use navigation::{
    FusionConfig, FusionService, GnssConfig, GnssService, HealthService, ImuConfig, ImuService,
};

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

    let imu = ImuService::new(inputs.imu).with_config(ImuConfig {
        period: Duration::from_millis(5),
    });
    let gnss = GnssService::new(inputs.gnss).with_config(GnssConfig {
        period: Duration::from_millis(20),
    });

    (
        fusion.start().unwrap(),
        imu.start().unwrap(),
        gnss.start().unwrap(),
    )
}

#[test]
fn active_services_own_hal_threads_and_lifecycle() {
    let (fusion, imu, gnss) = start_services();

    let mut health = HealthService::default();
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    let state = loop {
        let state = fusion.recv_timeout(Duration::from_millis(100)).unwrap();
        if state.imu_sequence != 0 && state.gnss_sequence != 0 {
            break state;
        }
        assert!(std::time::Instant::now() < deadline);
    };
    let snapshot = health.observe(state);
    assert!(snapshot.imu_alive && snapshot.gnss_alive);

    let imu_stats = imu.stop().unwrap();
    let gnss_stats = gnss.stop().unwrap();
    let fusion_stats = fusion.stop().unwrap();

    assert!(imu_stats.produced > 0);
    assert!(gnss_stats.produced > 0);
    assert!(fusion_stats.inputs >= 2);
}

#[test]
fn sensor_commands_are_processed_inside_the_service_loops() {
    let (fusion, imu, gnss) = start_services();

    std::thread::sleep(Duration::from_millis(35));
    let before = imu.status().unwrap();
    assert!(before.sampling);
    assert_eq!(before.period, Duration::from_millis(5));
    assert!(before.stats.produced > 0);

    imu.pause().unwrap();
    let paused = imu.status().unwrap();
    assert!(!paused.sampling);
    let paused_count = paused.stats.produced;
    std::thread::sleep(Duration::from_millis(25));
    assert_eq!(imu.status().unwrap().stats.produced, paused_count);

    imu.set_period(Duration::from_millis(2)).unwrap();
    imu.resume().unwrap();
    let resumed = imu.status().unwrap();
    assert!(resumed.sampling);
    assert_eq!(resumed.period, Duration::from_millis(2));
    std::thread::sleep(Duration::from_millis(20));
    assert!(imu.status().unwrap().stats.produced > paused_count);
    assert_eq!(
        imu.set_period(Duration::ZERO).unwrap_err().kind(),
        std::io::ErrorKind::InvalidInput
    );

    let gnss_before = gnss.status().unwrap();
    gnss.pause().unwrap();
    let gnss_paused = gnss.status().unwrap();
    assert!(!gnss_paused.sampling);
    assert!(gnss_paused.stats.produced >= gnss_before.stats.produced);
    gnss.set_period(Duration::from_millis(8)).unwrap();
    gnss.resume().unwrap();
    let gnss_resumed = gnss.status().unwrap();
    assert!(gnss_resumed.sampling);
    assert_eq!(gnss_resumed.period, Duration::from_millis(8));

    let imu_stats = imu.stop().unwrap();
    let gnss_stats = gnss.stop().unwrap();
    let fusion_stats = fusion.stop().unwrap();
    assert!(imu_stats.produced > 0);
    assert!(gnss_stats.produced > 0);
    assert!(fusion_stats.inputs > 0);
}
