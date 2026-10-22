#![cfg(all(feature = "pico-model", hal_enabled))]
use detector::{Config, Detector, DeviceError, Image};
const PERSON: &[u8] = include_bytes!("../../../tests/nuttx-std/detection/person.jpg");
const BACKGROUND: &[u8] = include_bytes!("../../../tests/nuttx-std/detection/background.jpg");
#[test]
fn common_api_reports_localization_and_rejects_invalid_frames_without_losing_owner() {
    let mut detector = detector::open(Config {
        require_localization: true,
        ..Config::default()
    })
    .unwrap();
    let capability = detector.capabilities();
    assert!(capability.localization);
    assert_eq!(
        (capability.model_width, capability.model_height),
        (224, 224)
    );
    let result = detector.detect(Image::jpeg(PERSON, 96, 96)).unwrap();
    assert_eq!(result.person_score.permille(), 940);
    assert!(!result.objects.is_empty());
    assert!(result.objects.len() <= capability.max_detections);
    for detection in result.objects.iter() {
        let b = detection.bounds;
        assert!(b.left < b.right && b.right <= 1000 && b.top < b.bottom && b.bottom <= 1000);
    }
    assert_eq!(
        detector.detect(Image::jpeg(PERSON, 95, 96)),
        Err(DeviceError::Frame)
    );
    assert_eq!(
        detector.detect(Image::jpeg(&[255, 216, 255, 217], 96, 96)),
        Err(DeviceError::Frame)
    );
    assert_eq!(
        detector.detect(Image::jpeg(PERSON, 96, 96)).unwrap(),
        result
    );
}
#[test]
fn two_model_owners_do_not_share_mutable_sdk_state() {
    let mut first = detector::open(Config::default()).unwrap();
    let mut second = detector::open(Config::default()).unwrap();
    for _ in 0..4 {
        assert_eq!(
            first
                .detect(Image::jpeg(PERSON, 96, 96))
                .unwrap()
                .person_score
                .permille(),
            940
        );
        assert_eq!(
            second
                .detect(Image::jpeg(BACKGROUND, 96, 96))
                .unwrap()
                .person_score
                .permille(),
            415
        );
    }
    drop(first);
    assert_eq!(
        second
            .detect(Image::jpeg(PERSON, 96, 96))
            .unwrap()
            .person_score
            .permille(),
        940
    );
}

#[cfg(feature = "qualification")]
#[test]
fn qualification_observes_actual_results_and_resets_timings_after_invalid_input() {
    use detector::qualification::Profiled;
    let mut plain = detector::open(Config::default()).unwrap();
    let mut measured = detector::qualification::open(Config::default()).unwrap();
    for jpeg in [PERSON, BACKGROUND, PERSON] {
        let image = Image::jpeg(jpeg, 96, 96);
        assert_eq!(plain.detect(image), measured.detect(image));
        let t = measured.timings();
        assert!(t.kernel_us > 0);
        assert!(t.total_us >= t.decode_us + t.resize_us + t.kernel_us + t.postprocess_us);
    }
    assert_eq!(
        measured.detect(Image::jpeg(PERSON, 95, 96)),
        Err(DeviceError::Frame)
    );
    let t = measured.timings();
    assert_eq!((t.resize_us, t.kernel_us, t.postprocess_us), (0, 0, 0));
    assert_eq!(
        measured
            .detect(Image::jpeg(PERSON, 96, 96))
            .unwrap()
            .person_score
            .permille(),
        940
    );
}
