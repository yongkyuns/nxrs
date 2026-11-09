//! Exercise the public API with the unbound, mock and simulation profiles.
use imu::{Config, Device, DeviceError, Session};

#[test]
fn device_methods_validate_before_acquisition_and_release_after_join() {
    let device = imu_devices::primary_imu();
    let config = match device.default_config() {
        Ok(config) => config,
        Err(DeviceError::Unsupported) => {
            let config = Config {
                min_rate_millihz: 100_000,
                max_rate_millihz: 100_000,
                accel_range_g: 4,
                gyro_range_dps: 2000,
                max_delivery_interval_us: 10_000,
            };
            assert_eq!(device.resolve(config), Err(DeviceError::Unsupported));
            assert_eq!(
                device.open(config, |_| panic!("unbound source")).err(),
                Some(DeviceError::Unsupported)
            );
            return;
        }
        other => panic!("unexpected configuration: {other:?}"),
    };
    let invalid = Config {
        min_rate_millihz: 0,
        ..config
    };
    assert_eq!(
        device.open(invalid, |_| panic!("invalid source")).err(),
        Some(DeviceError::InvalidData)
    );
    let (mut session, applied) = device.open(config, |_| Ok(())).unwrap();
    assert_eq!(device.resolve(config), Ok(applied));
    assert_eq!(
        device.open_default(|_| Ok(())).err(),
        Some(DeviceError::Busy)
    );
    session.request_stop().unwrap();
    session.join().unwrap();
    assert!(session.is_finished());
    device.open_default(|_| Ok(())).unwrap().join().unwrap();
}
