#[cfg(any(
    not(hal_enabled),
    not(any(feature = "person-model", feature = "pico-model"))
))]
#[test]
fn unbound_contract_does_not_implicitly_open_a_mock() {
    use detector::{Config, DeviceError};
    assert!(matches!(
        detector::open(Config::default()),
        Err(DeviceError::Unsupported)
    ));
}

#[cfg(all(hal_enabled, feature = "person-model", not(feature = "pico-model")))]
#[test]
fn presence_provider_reports_its_limits_and_rejects_required_localization() {
    use detector::{Config, Detector, DeviceError, Image};
    assert!(matches!(
        detector::open(Config {
            require_localization: true,
            ..Config::default()
        }),
        Err(DeviceError::Unsupported)
    ));
    let mut model = detector::open(Config::default()).unwrap();
    let capabilities = model.capabilities();
    assert!(!capabilities.localization);
    assert_eq!(
        (capabilities.model_width, capabilities.model_height),
        (96, 96)
    );
    let jpeg = include_bytes!("../../../tests/nuttx-std/detection/person.jpg");
    let result = model.detect(Image::jpeg(jpeg, 96, 96)).unwrap();
    assert!(result.person_score.permille() >= 500);
    assert!(result.objects.is_empty());
}

#[cfg(all(feature = "probe", feature = "pico-model", not(hal_enabled)))]
#[test]
fn unbound_qualification_does_not_acquire_an_engine() {
    use detector::{Config, DeviceError};
    assert!(matches!(
        detector::qualification::open(Config::default()),
        Err(DeviceError::Unsupported)
    ));
}
