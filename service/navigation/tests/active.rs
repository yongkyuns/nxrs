use imu::Device;
use std::time::Duration;

use navigation::{ControlRequest, RequestError};
use navigation::{FusionConfig, FusionService, GnssService, HealthService, ImuService};

// These integration cases use the profile's same canonical primary device.
// Hold the fixture through joined service shutdown, not just startup.
static PRIMARY: std::sync::Mutex<()> = std::sync::Mutex::new(());

#[test]
fn publication_open_failure_remains_terminal_after_the_callback_is_dropped() {
    use imu::Session;
    use navigation::observations::{publication, ObservationService, Reading, SensorFault};
    let _primary = PRIMARY.lock().unwrap();
    let device = composition::imu::primary_imu();
    let mut occupied = device.open_default(|_| Ok(())).unwrap();
    let (publishers, [owner, mut other]) = publication::<1, 2>(Duration::from_secs(1), None);
    let [mut publisher] = publishers.imu;
    let mut service = ObservationService::with_reader(owner);
    let (sender, _inbox) = messaging::bounded::<()>(1);
    assert_eq!(
        service.start_imu_with(0, device, sender.clone(), move |event| {
            let _ = publisher.record(&event);
            None
        }),
        Err(SensorFault::Imu(0, imu::DeviceError::Busy))
    );
    occupied.request_stop().unwrap();
    occupied.join().unwrap();
    assert_eq!(
        service.snapshot(std::time::Instant::now()).imu,
        [Reading::Fault]
    );
    assert_eq!(
        other.snapshot(std::time::Instant::now()).imu,
        [Reading::Fault]
    );
    // Releasing the physical device cannot restart this failed publication lifetime.
    assert_eq!(
        service.start_imu_with(0, device, sender, |_| None),
        Err(SensorFault::Imu(0, imu::DeviceError::Busy))
    );
}

#[test]
fn retained_commands_share_the_callers_existing_wait_point() {
    let _primary = PRIMARY.lock().unwrap();
    let caller = std::thread::current();
    let wake = move || caller.unpark();
    let (fusion, inputs) = FusionService::new();
    let fusion = fusion.start_with_wake(wake.clone()).unwrap();
    let mut imu = ImuService::new(inputs.imu)
        .start_on_with_wake(composition::imu::primary_imu(), wake.clone())
        .unwrap();
    let mut gnss = GnssService::new(inputs.gnss)
        .start_on_with_wake(composition::gnss::primary_gnss(), wake)
        .unwrap();

    for request in [
        ControlRequest::Pause,
        ControlRequest::Resume,
        ControlRequest::Status,
    ] {
        let imu_id = imu.try_request(request).unwrap();
        let gnss_id = gnss.try_request(request).unwrap();
        assert_eq!(
            imu.try_request(ControlRequest::Status),
            Err(RequestError::Busy)
        );
        assert_eq!(
            gnss.try_request(ControlRequest::Status),
            Err(RequestError::Busy)
        );
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        let mut imu_reply = None;
        let mut gnss_reply = None;
        loop {
            imu_reply = imu_reply.or_else(|| imu.try_completion());
            gnss_reply = gnss_reply.or_else(|| gnss.try_completion());
            // Data and completions share the wake, but retain independent capacity.
            // A bounded drain must continue without parking when it exhausts its budget.
            let mut drain_exhausted = true;
            for _ in 0..4 {
                if fusion.try_recv().is_err() {
                    drain_exhausted = false;
                    break;
                }
            }
            if imu_reply.is_some() && gnss_reply.is_some() {
                break;
            }
            let now = std::time::Instant::now();
            assert!(now < deadline, "control reply did not arrive");
            if !drain_exhausted {
                std::thread::park_timeout(deadline.saturating_duration_since(now));
            }
        }
        let imu_reply = imu_reply.unwrap();
        let gnss_reply = gnss_reply.unwrap();
        assert_eq!(imu_reply.id, imu_id);
        assert_eq!(gnss_reply.id, gnss_id);
        assert_eq!(imu_reply.request, request);
        assert_eq!(gnss_reply.request, request);
        assert_eq!(
            imu_reply.result.unwrap().sampling,
            request != ControlRequest::Pause
        );
        assert_eq!(
            gnss_reply.result.unwrap().sampling,
            request != ControlRequest::Pause
        );
    }
    imu.stop().unwrap();
    gnss.stop().unwrap();
    fusion.stop().unwrap();
}

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
        imu.start_on(composition::imu::primary_imu()).unwrap(),
        gnss.start_on(composition::gnss::primary_gnss()).unwrap(),
    )
}

#[test]
fn active_services_own_hal_threads_and_lifecycle() {
    let _primary = PRIMARY.lock().unwrap();
    let (fusion, imu, gnss) = start_services();
    assert_eq!(
        composition::imu::primary_imu()
            .open_default(|_| Ok(()))
            .err(),
        Some(imu::DeviceError::Busy)
    );
    let (other_fusion, other_inputs) = FusionService::new();
    let other_fusion = other_fusion.start().unwrap();
    let other_imu = ImuService::new(other_inputs.imu)
        .start_on(composition::imu::secondary())
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
    composition::imu::primary_imu()
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
