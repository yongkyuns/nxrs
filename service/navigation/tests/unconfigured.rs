use std::io::ErrorKind;

use navigation::{FusionService, GnssService, ImuService};

#[test]
fn sensor_services_fail_without_selected_providers() {
    let (_fusion, inputs) = FusionService::new();

    let imu_error = match ImuService::new(inputs.imu).start_on(composition::imu::primary_imu()) {
        Ok(_) => panic!("IMU unexpectedly started without a selected provider"),
        Err(error) => error,
    };
    let gnss_error = match GnssService::new(inputs.gnss).start_on(composition::gnss::primary_gnss()) {
        Ok(_) => panic!("GNSS unexpectedly started without a HAL platform"),
        Err(error) => error,
    };

    assert_eq!(imu_error.kind(), ErrorKind::Unsupported);
    assert_eq!(gnss_error.kind(), ErrorKind::Unsupported);
}
