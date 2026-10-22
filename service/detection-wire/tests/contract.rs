use detection_wire::*;
fn golden() -> Snapshot {
    serde_json::from_slice(include_bytes!(
        "../../../clients/ios/Tests/NxrsMonitorCoreTests/Fixtures/detection.json"
    ))
    .unwrap()
}
#[test]
fn shared_phone_fixture_roundtrips_through_bounded_source_storage() {
    let source = golden().source.unwrap();
    source.validate().unwrap();
    let mut bytes = [0; MAX_SOURCE_BYTES];
    let n = source.encode(&mut bytes).unwrap();
    assert_eq!(Source::decode(&bytes[..n]).unwrap(), source);
    assert_eq!(
        source
            .latest
            .unwrap()
            .objects
            .iter()
            .next()
            .unwrap()
            .bounds
            .left,
        100
    );
}
#[test]
fn malformed_geometry_clock_confidence_version_and_excess_lists_are_rejected() {
    let source = golden().source.unwrap();
    for change in 0..5 {
        let mut s = source.clone();
        let o = s.latest.as_mut().unwrap();
        match change {
            0 => o.objects.0[0].as_mut().unwrap().bounds.left = 999,
            1 => o.person_score_permille = 1001,
            2 => o.captured_ms = 1001,
            3 => s.event_count = 2,
            _ => o.frame_sequence = 0,
        }
        assert!(s.validate().is_err());
    }
    let mut value = serde_json::to_value(source).unwrap();
    value["latest"]["objects"] =
        serde_json::Value::Array(vec![value["latest"]["objects"][0].clone(); 11]);
    assert!(Source::decode(&serde_json::to_vec(&value).unwrap()).is_err());
    value["api_version"] = "2".into();
    assert!(Source::decode(&serde_json::to_vec(&value).unwrap()).is_err());
    assert!(Source::decode(&vec![b' '; MAX_SOURCE_BYTES + 1]).is_err());
}
#[test]
fn worst_case_full_boxes_and_event_ring_fit_the_camera_buffer() {
    let mut source = golden().source.unwrap();
    let b = *source
        .latest
        .as_ref()
        .unwrap()
        .objects
        .iter()
        .next()
        .unwrap();
    source.latest.as_mut().unwrap().objects = Bounded::default();
    for _ in 0..10 {
        source.latest.as_mut().unwrap().objects.push(b).unwrap();
    }
    source.events = Bounded::default();
    source.event_count = 8;
    for id in 1..=8 {
        source
            .events
            .push(Event {
                id,
                at_ms: 1000,
                kind: EventKind::PersonEntered,
            })
            .unwrap();
    }
    let mut output = [0; MAX_SOURCE_BYTES];
    assert!(source.encode(&mut output).unwrap() < MAX_SOURCE_BYTES);
    assert!(source.encode(&mut [0; 10]).is_err());
}

#[test] fn fresh_unconfirmed_observation_is_not_fabricated_as_clear() {
 let mut source=golden().source.unwrap();source.presence=Presence::Unavailable;source.validate().unwrap();
}
