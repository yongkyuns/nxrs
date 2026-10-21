use detection_service::{Error, Event, Monitor, Observation, Policy, Presence, Score};
fn observation(sequence: u64, captured_ms: u64, score: u16) -> Observation {
    Observation {
        sequence,
        captured_ms,
        score: Score::new(score).unwrap(),
        inference_us: 17,
    }
}
#[test]
fn evidence_hysteresis_and_expiry_have_distinct_security_meanings() {
    let mut m = Monitor::new(Policy::default()).unwrap();
    assert_eq!(m.presence(), Presence::Unavailable);
    assert_eq!(m.observe(observation(1, 10, 900), 11), Ok(None));
    assert_eq!(m.observe(observation(2, 20, 400), 21), Ok(None));
    assert_eq!(m.observe(observation(3, 30, 900), 31), Ok(None));
    assert_eq!(
        m.observe(observation(4, 40, 900), 41),
        Ok(Some(Event::PersonEntered))
    );
    assert_eq!(m.observe(observation(5, 50, 400), 51), Ok(None));
    assert_eq!(m.presence(), Presence::Person);
    assert_eq!(m.observe(observation(6, 60, 100), 61), Ok(None));
    assert_eq!(
        m.observe(observation(7, 70, 100), 71),
        Ok(Some(Event::PersonLeft))
    );
    assert_eq!(m.presence(), Presence::Clear);
    assert_eq!(m.tick(5071), Ok(Some(Event::Unavailable)));
    assert_eq!(m.tick(6000), Ok(None));
    // Losing availability does not reset source order/replay protection.
    assert_eq!(m.observe(observation(7, 70, 900), 6001), Err(Error::Frame));
}
#[test]
fn stale_results_and_large_sampling_gaps_cannot_confirm_person_presence() {
    let mut m = Monitor::new(Policy::default()).unwrap();
    m.observe(observation(1, 0, 900), 1).unwrap();
    assert_eq!(m.observe(observation(2, 4500, 900), 4501), Ok(None));
    assert_eq!(m.presence(), Presence::Unavailable);
    assert_eq!(m.observe(observation(3, 4600, 900), 10000), Ok(None));
    assert_eq!(m.presence(), Presence::Unavailable);
    assert_eq!(m.observe(observation(4, 10001, 900), 10001), Ok(None));
    assert_eq!(
        m.observe(observation(5, 10002, 900), 10002),
        Ok(Some(Event::PersonEntered))
    );
    assert_eq!(m.unavailable(), Some(Event::Unavailable));
    assert_eq!(m.presence(), Presence::Unavailable);
}
#[test]
fn rejects_bad_scores_configuration_and_clock_metadata() {
    assert_eq!(Score::new(1001), Err(Error::Model));
    assert!(Monitor::new(Policy {
        confirmation_samples: 0,
        ..Policy::default()
    })
    .is_err());
    let mut m = Monitor::new(Policy::default()).unwrap();
    m.tick(100).unwrap();
    assert_eq!(m.tick(99), Err(Error::Clock));
    assert_eq!(m.observe(observation(1, 102, 900), 101), Err(Error::Frame));
}
