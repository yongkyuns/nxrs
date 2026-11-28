use super::*;
use std::time::UNIX_EPOCH;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Call {
    Set(SystemTime),
    Replace(i64),
    Query,
}

struct Fake {
    rate: Option<u32>,
    pending: i64,
    calls: Vec<Call>,
    fail: bool,
}

impl Fake {
    fn new(rate: Option<u32>) -> Self {
        Self {
            rate,
            pending: 0,
            calls: Vec::new(),
            fail: false,
        }
    }

    fn record(&mut self, call: Call) -> Result<(), time::Error> {
        self.calls.push(call);
        if self.fail {
            Err(time::Error::Io)
        } else {
            Ok(())
        }
    }
}

impl Control for Fake {
    fn capabilities(&self) -> time::Capabilities {
        time::Capabilities {
            slew_rate_ppm: self.rate,
        }
    }

    fn set(&mut self, utc: SystemTime) -> Result<(), time::Error> {
        self.record(Call::Set(utc))?;
        self.pending = 0;
        Ok(())
    }

    fn replace_adjustment(&mut self, us: i64) -> Result<i64, time::Error> {
        self.record(Call::Replace(us))?;
        Ok(std::mem::replace(&mut self.pending, us))
    }

    fn pending_adjustment(&mut self) -> Result<i64, time::Error> {
        self.record(Call::Query)?;
        Ok(self.pending)
    }
}

impl Control for &mut Fake {
    fn capabilities(&self) -> time::Capabilities {
        (**self).capabilities()
    }
    fn set(&mut self, utc: SystemTime) -> Result<(), time::Error> {
        (**self).set(utc)
    }
    fn replace_adjustment(&mut self, us: i64) -> Result<i64, time::Error> {
        (**self).replace_adjustment(us)
    }
    fn pending_adjustment(&mut self) -> Result<i64, time::Error> {
        (**self).pending_adjustment()
    }
}

fn policy() -> Policy {
    Policy {
        accuracy: Duration::from_millis(1),
        refresh_interval: Duration::from_secs(10),
        max_holdover: Duration::from_secs(60),
        max_slew_time: Duration::from_secs(60),
        clock_resolution: Duration::from_micros(1),
        drift_ppm: 10,
    }
}

type TestController = Controller<Fake, 3>;

fn controller(initial: InitialTime) -> TestController {
    let mut controller = Controller::new(
        Fake::new(Some(500)),
        policy(),
        [
            Source { priority: 0 },
            Source { priority: 1 },
            Source { priority: 0 },
        ],
        initial,
    )
    .unwrap();
    assert_eq!(controller.control.calls, [Call::Replace(0)]);
    controller.control.calls.clear();
    controller
}

fn utc() -> SystemTime {
    UNIX_EPOCH + Duration::from_secs(1_800_000_000)
}

fn observation(c: &TestController, source: usize, at: Instant, utc: SystemTime) -> Observation {
    Observation {
        source,
        epoch: c.epoch(),
        at,
        utc,
        uncertainty: Duration::from_micros(10),
    }
}

fn reading(at: Instant, utc: SystemTime) -> Reading {
    Reading {
        at,
        utc,
        uncertainty: Duration::from_micros(2),
    }
}

fn feed(c: &mut TestController, observation: Observation, read: Reading) -> Result<Status, Error> {
    c.admit(observation, read.at)?;
    check(c, read)
}

fn check(c: &mut TestController, read: Reading) -> Result<Status, Error> {
    let pending = c.pending()?;
    c.reconcile(read, pending)
}

#[test]
fn seed_is_once_and_requires_an_independent_readback() {
    let mut c = controller(InitialTime::Unknown);
    let at = c.minimum_at;
    let sample = observation(&c, 0, at, utc());
    let seeded = feed(&mut c, sample, reading(at, UNIX_EPOCH)).unwrap();
    assert_eq!(seeded.state, State::Checking);
    assert!(seeded.initialized);
    assert_eq!(seeded.error_bound, None);
    assert_eq!(c.control.calls, [Call::Query, Call::Set(utc())]);
    assert_eq!(
        check(&mut c, reading(at, utc())).unwrap().state,
        State::Synchronized
    );

    // Loss of freshness or all source evidence never grants another seed step.
    let later = at + Duration::from_secs(61);
    assert_eq!(
        check(&mut c, reading(later, utc())).unwrap().state,
        State::Holdover
    );
    assert!(c.last_status().initialized);
    let next = observation(&c, 0, later, utc() + Duration::from_secs(61));
    assert_eq!(
        feed(&mut c, next, reading(later, utc())).unwrap_err(),
        Error::StepRequired {
            offset_us: 61_000_000
        }
    );
    assert_eq!(
        c.control
            .calls
            .iter()
            .filter(|call| matches!(call, Call::Set(_)))
            .count(),
        1
    );
}

#[test]
fn delayed_uncertain_initial_sample_preserves_the_one_seed() {
    let mut c = controller(InitialTime::Unknown);
    c.policy.drift_ppm = 1_000;
    let at = c.minimum_at;
    let delayed = at + Duration::from_secs(2);
    let sample = observation(&c, 0, at, utc());
    let status = feed(&mut c, sample, reading(delayed, UNIX_EPOCH)).unwrap();
    assert_eq!(status.state, State::Unknown);
    assert!(!status.initialized);
    assert!(c.seed_allowed);
    assert!(!c
        .control
        .calls
        .iter()
        .any(|call| matches!(call, Call::Set(_))));
    let fresh = observation(&c, 0, delayed, utc() + Duration::from_secs(2));
    assert_eq!(
        feed(&mut c, fresh, reading(delayed, UNIX_EPOCH))
            .unwrap()
            .state,
        State::Checking
    );
    assert_eq!(c.control.calls.last(), Some(&Call::Set(fresh.utc)));
}

#[test]
fn resume_before_first_synchronization_is_still_unknown() {
    let mut c = controller(InitialTime::Unknown);
    c.suspend().unwrap();
    c.resume().unwrap();
    assert_eq!(c.last_status().state, State::Unknown);
    assert!(!c.last_status().initialized);
    assert!(c.seed_allowed);
}

#[test]
fn input_rejection_does_not_mutate_the_clock_or_replace_evidence() {
    let mut c = controller(InitialTime::Preserve);
    let at = c.minimum_at + Duration::from_secs(2);
    let good = observation(&c, 0, at, utc());
    feed(&mut c, good, reading(at, utc())).unwrap();
    c.control.calls.clear();
    let cases = [
        (Observation { source: 3, ..good }, at, Error::InvalidSource),
        (
            Observation {
                epoch: Epoch(9),
                ..good
            },
            at,
            Error::WrongEpoch,
        ),
        (
            Observation {
                at: at + Duration::from_secs(1),
                ..good
            },
            at,
            Error::Future,
        ),
        (good, at + Duration::from_secs(61), Error::Stale),
        (good, at, Error::Reordered),
        (
            Observation {
                at: at - Duration::from_secs(1),
                ..good
            },
            at,
            Error::Reordered,
        ),
        (
            Observation {
                at: at + Duration::from_secs(1),
                uncertainty: Duration::from_secs(1),
                ..good
            },
            at + Duration::from_secs(1),
            Error::Uncertain,
        ),
        (
            Observation {
                at: c.minimum_at - Duration::from_nanos(1),
                ..good
            },
            at,
            Error::WrongEpoch,
        ),
    ];
    for (sample, now, expected) in cases {
        assert_eq!(c.admit(sample, now), Err(expected));
    }
    assert!(c.control.calls.is_empty());
    assert_eq!(c.observations[0].unwrap().at, good.at);
}

#[test]
fn replacement_uses_actual_error_without_double_counting_pending() {
    for sign in [1_i64, -1] {
        let mut c = controller(InitialTime::Preserve);
        let at = c.minimum_at;
        let shift = |base: SystemTime, us: u64| {
            if sign > 0 {
                base + Duration::from_micros(us)
            } else {
                base - Duration::from_micros(us)
            }
        };
        let first = observation(&c, 0, at, shift(utc(), 8_000));
        assert_eq!(
            feed(&mut c, first, reading(at, utc())).unwrap().state,
            State::Adjusting
        );
        assert_eq!(c.control.pending, sign * 8_000);
        // 3ms applied. New evidence measures 6ms, with 5ms still pending.
        c.control.pending = sign * 5_000;
        let next_at = at + Duration::from_secs(1);
        let wall = shift(utc() + Duration::from_secs(1), 3_000);
        let next = observation(&c, 0, next_at, shift(wall, 6_000));
        feed(&mut c, next, reading(next_at, wall)).unwrap();
        assert_eq!(c.control.calls.last(), Some(&Call::Replace(sign * 6_000)));
        assert_eq!(c.control.pending, sign * 6_000);
    }
}

#[test]
fn matching_residual_is_not_restarted_and_zero_is_not_proof_of_alignment() {
    let mut c = controller(InitialTime::Preserve);
    let at = c.minimum_at;
    let sample = observation(&c, 0, at, utc() + Duration::from_millis(5));
    feed(&mut c, sample, reading(at, utc())).unwrap();
    c.control.calls.clear();
    let status = check(&mut c, reading(at, utc())).unwrap();
    assert_eq!(status.state, State::Adjusting);
    assert_eq!(c.control.calls, [Call::Query]);
    c.control.pending = 0;
    assert_eq!(
        check(&mut c, reading(at, utc())).unwrap().state,
        State::Adjusting
    );
    assert_eq!(c.control.calls.last(), Some(&Call::Replace(5_000)));
    c.control.pending = 0;
    assert_eq!(
        check(&mut c, reading(at, sample.utc)).unwrap().state,
        State::Synchronized
    );
}

#[test]
fn entering_deadband_cancels_remaining_correction_before_alignment() {
    let mut c = controller(InitialTime::Preserve);
    let at = c.minimum_at;
    let sample = observation(&c, 0, at, utc());
    c.control.pending = -5_000;
    let status = feed(&mut c, sample, reading(at, utc())).unwrap();
    assert_eq!(c.control.calls.last(), Some(&Call::Replace(0)));
    assert_eq!(status.state, State::Checking);
    assert_eq!(status.error_bound, None);
    assert_eq!(
        check(&mut c, reading(at, utc())).unwrap().state,
        State::Synchronized
    );
}

#[test]
fn signed_large_surprises_stop_slew_without_stepping() {
    for negative in [false, true] {
        let mut c = controller(InitialTime::Preserve);
        let at = c.minimum_at;
        c.control.pending = 5_000;
        let target = if negative {
            utc() - Duration::from_secs(5)
        } else {
            utc() + Duration::from_secs(5)
        };
        let sample = observation(&c, 0, at, target);
        assert_eq!(
            feed(&mut c, sample, reading(at, utc())).unwrap_err(),
            Error::StepRequired {
                offset_us: if negative { -5_000_000 } else { 5_000_000 }
            }
        );
        assert_eq!(c.last_status().state, State::StepRequired);
        assert!(c.last_status().initialized);
        assert_eq!(c.control.pending, 0);
        assert!(!c
            .control
            .calls
            .iter()
            .any(|call| matches!(call, Call::Set(_))));
    }
}

#[test]
fn uncertainty_ages_and_historical_status_cannot_claim_current_quality() {
    let mut c = controller(InitialTime::Preserve);
    let at = c.minimum_at;
    let sample = observation(&c, 0, at, utc());
    let initial = feed(&mut c, sample, reading(at, utc())).unwrap();
    assert_eq!(initial.error_bound, Some(Duration::from_micros(13)));
    assert_eq!(initial.refresh_at, at + Duration::from_secs(10));
    assert_eq!(c.last_status().checked_at, at);
    let later = at + Duration::from_secs(10);
    let held = check(&mut c, reading(later, utc() + Duration::from_secs(10))).unwrap();
    assert_eq!(held.state, State::Holdover);
    assert_eq!(held.error_bound, Some(Duration::from_nanos(113_002)));
    assert_eq!(held.reference_age, Some(Duration::from_secs(10)));
    let expired = at + Duration::from_secs(61);
    c.control.pending = 100;
    let status = check(&mut c, reading(expired, utc() + Duration::from_secs(61))).unwrap();
    assert_eq!(status.state, State::Holdover);
    assert_eq!(status.error_bound, None);
    assert_eq!(c.control.pending, 0);
}

#[test]
fn drift_can_request_renewal_before_the_periodic_deadline() {
    let mut c = controller(InitialTime::Preserve);
    c.policy.drift_ppm = 1_000;
    let at = c.minimum_at;
    let sample = observation(&c, 0, at, utc());
    let status = feed(&mut c, sample, reading(at, utc())).unwrap();
    assert_eq!(status.refresh_at, at + Duration::from_micros(987_012));
}

#[test]
fn priority_and_freshness_are_explicit_and_equal_priority_conflict_stops_correction() {
    let mut c = controller(InitialTime::Preserve);
    let at = c.minimum_at;
    let preferred = observation(&c, 0, at, utc() + Duration::from_millis(5));
    feed(&mut c, preferred, reading(at, utc())).unwrap();
    let fallback = observation(&c, 1, at, utc() + Duration::from_secs(1));
    assert_eq!(
        feed(&mut c, fallback, reading(at, utc())).unwrap().source,
        Some(0)
    );
    let conflicting = observation(&c, 2, at, utc() - Duration::from_millis(5));
    assert_eq!(
        feed(&mut c, conflicting, reading(at, utc())).unwrap_err(),
        Error::Conflict
    );
    assert_eq!(c.last_status().state, State::Conflict);
    assert_eq!(c.control.pending, 0);
    // A fresh fallback wins over old preferred sources; old disagreement does
    // not permanently prevent recovery from a genuinely new observation.
    let later = at + Duration::from_secs(11);
    let fallback = observation(&c, 1, later, utc() + Duration::from_secs(11));
    let status = feed(&mut c, fallback, reading(later, fallback.utc)).unwrap();
    assert_eq!(status.source, Some(1));
    assert_eq!(status.state, State::Synchronized);
}

#[test]
fn three_sources_must_share_a_common_possible_utc() {
    let mut c = controller(InitialTime::Preserve);
    c.sources.fill(Source { priority: 0 });
    let at = c.minimum_at;
    // Each wide interval overlaps the best narrow interval, but they do not
    // overlap each other. Comparing only against the best misses this conflict.
    let intervals = [
        (utc(), Duration::from_micros(1)),
        (
            utc() - Duration::from_micros(4),
            Duration::from_nanos(4_500),
        ),
        (
            utc() + Duration::from_micros(4),
            Duration::from_nanos(2_500),
        ),
    ];
    for (source, (utc, uncertainty)) in intervals.into_iter().enumerate() {
        c.admit(
            Observation {
                uncertainty,
                ..observation(&c, source, at, utc)
            },
            at,
        )
        .unwrap();
    }
    assert_eq!(
        check(&mut c, reading(at, utc())).unwrap_err(),
        Error::Conflict
    );
    assert_eq!(c.last_status().state, State::Conflict);
}

#[test]
fn expired_source_overflow_cannot_poison_a_fresh_fallback() {
    let mut c = controller(InitialTime::Preserve);
    let at = c.minimum_at;
    let far_utc = UNIX_EPOCH
        .checked_add(Duration::from_secs(i64::MAX as u64 - 10))
        .unwrap();
    c.admit(observation(&c, 0, at, far_utc), at).unwrap();
    let later = at + Duration::from_secs(61);
    assert!(far_utc.checked_add(Duration::from_secs(61)).is_none());
    let fresh = observation(&c, 1, later, utc());
    let status = feed(&mut c, fresh, reading(later, utc())).unwrap();
    assert_eq!(status.source, Some(1));
    assert_eq!(status.state, State::Synchronized);
}

#[test]
fn backend_failures_do_not_claim_the_requested_change_or_restore_seed_permission() {
    let mut c = controller(InitialTime::Unknown);
    let at = c.minimum_at;
    let sample = observation(&c, 0, at, utc());
    c.admit(sample, at).unwrap();
    c.control.fail = true;
    assert_eq!(
        c.reconcile(reading(at, UNIX_EPOCH), 0).unwrap_err(),
        Error::Backend(time::Error::Io)
    );
    assert_eq!(c.last_status().state, State::Fault);
    assert!(!c.last_status().initialized);
    assert!(!c.seed_allowed);
    c.control.fail = false;
    assert!(matches!(
        check(&mut c, reading(at, UNIX_EPOCH)),
        Err(Error::StepRequired { .. })
    ));
    assert_eq!(
        c.control
            .calls
            .iter()
            .filter(|call| matches!(call, Call::Set(_)))
            .count(),
        1
    );

    for pending in [0, 5_000] {
        let mut c = controller(InitialTime::Preserve);
        let at = c.minimum_at;
        let target = if pending == 0 {
            utc() + Duration::from_millis(5)
        } else {
            utc()
        };
        c.admit(observation(&c, 0, at, target), at).unwrap();
        c.control.fail = true;
        assert_eq!(
            c.reconcile(reading(at, utc()), pending).unwrap_err(),
            Error::Backend(time::Error::Io)
        );
        assert_eq!(c.last_status().state, State::Fault);
        assert_eq!(c.last_status().pending_us, None);
        assert_eq!(c.last_status().error_bound, None);
        assert!(c.last_status().initialized);
    }
    let mut c = controller(InitialTime::Preserve);
    c.control.fail = true;
    assert_eq!(c.poll().unwrap_err(), Error::Backend(time::Error::Io));
    assert_eq!(c.last_status().state, State::Fault);
}

#[test]
fn suspend_failure_invalidates_quality_and_resume_rejects_delayed_observations() {
    let mut c = controller(InitialTime::Preserve);
    let at = c.minimum_at;
    let old = observation(&c, 0, at, utc());
    feed(&mut c, old, reading(at, utc())).unwrap();
    assert_eq!(c.resume(), Err(Error::NotSuspended));
    c.control.fail = true;
    assert_eq!(c.suspend(), Err(Error::Backend(time::Error::Io)));
    assert_eq!(c.last_status().state, State::Suspended);
    assert!(c.observations.iter().all(Option::is_none));
    assert_eq!(c.observe(old).unwrap_err(), Error::Suspended);
    assert_eq!(c.resume(), Err(Error::Backend(time::Error::Io)));
    c.control.fail = false;
    let epoch = c.resume().unwrap();
    assert_ne!(old.epoch, epoch);
    assert!(c.last_status().initialized);
    assert_eq!(c.observe(old).unwrap_err(), Error::WrongEpoch);
    let relabeled = Observation { epoch, ..old };
    assert_eq!(c.observe(relabeled).unwrap_err(), Error::WrongEpoch);
    let at = c.minimum_at;
    let fresh = observation(&c, 0, at, utc() + Duration::from_secs(1));
    assert_eq!(
        feed(&mut c, fresh, reading(at, utc())).unwrap_err(),
        Error::StepRequired {
            offset_us: 1_000_000
        }
    );
}

#[test]
fn step_only_provider_can_seed_but_never_substitutes_steps_for_slewing() {
    let mut c = Controller::new(
        Fake::new(None),
        policy(),
        [Source { priority: 0 }; 3],
        InitialTime::Unknown,
    )
    .unwrap();
    assert!(c.control.calls.is_empty());
    let at = c.minimum_at;
    let sample = observation(&c, 0, at, utc());
    feed(&mut c, sample, reading(at, UNIX_EPOCH)).unwrap();
    assert_eq!(c.control.calls, [Call::Set(utc())]);
    assert_eq!(
        check(&mut c, reading(at, utc())).unwrap().state,
        State::Synchronized
    );
    assert_eq!(
        check(&mut c, reading(at, utc() - Duration::from_millis(5))).unwrap_err(),
        Error::StepRequired { offset_us: 5_000 }
    );
    c.suspend().unwrap();
    c.resume().unwrap();
    assert_eq!(c.control.calls, [Call::Set(utc())]);
}

#[test]
fn invalid_policy_arithmetic_and_excessive_read_brackets_fail_conservatively() {
    assert!(matches!(
        Controller::new(Fake::new(Some(500)), policy(), [], InitialTime::Unknown),
        Err(Error::InvalidPolicy)
    ));
    for policy in [
        Policy {
            accuracy: Duration::ZERO,
            ..policy()
        },
        Policy {
            refresh_interval: Duration::ZERO,
            ..policy()
        },
        Policy {
            max_holdover: Duration::from_secs(1),
            ..policy()
        },
        Policy {
            clock_resolution: Duration::from_millis(1),
            ..policy()
        },
        Policy {
            drift_ppm: 1_000_001,
            ..policy()
        },
        Policy {
            max_slew_time: Duration::MAX,
            ..policy()
        },
    ] {
        assert!(matches!(
            Controller::new(
                Fake::new(Some(500)),
                policy,
                [Source { priority: 0 }],
                InitialTime::Unknown
            ),
            Err(Error::InvalidPolicy)
        ));
    }
    assert!(matches!(
        Controller::new(
            Fake::new(Some(0)),
            policy(),
            [Source { priority: 0 }],
            InitialTime::Unknown
        ),
        Err(Error::InvalidPolicy)
    ));
    assert_eq!(nanos(u128::MAX), Err(Error::OutOfRange));
    assert_eq!(scale(Duration::MAX, 1_000_000, 1), Err(Error::OutOfRange));
    assert_eq!(
        scale(Duration::from_nanos(1), 1, 1_000_000),
        Ok(Duration::from_nanos(1))
    );
    let mut c = controller(InitialTime::Preserve);
    let at = c.minimum_at;
    c.admit(observation(&c, 0, at, utc()), at).unwrap();
    assert_eq!(
        c.reconcile(
            Reading {
                uncertainty: Duration::from_secs(1),
                ..reading(at, utc())
            },
            0
        )
        .unwrap_err(),
        Error::Uncertain
    );
    assert_eq!(c.last_status().state, State::Fault);
    assert!(c.control.calls.is_empty());
}

#[test]
fn invalid_constructor_horizon_cannot_cancel_an_existing_adjustment() {
    let mut control = Fake::new(Some(500));
    control.pending = 8_000;
    assert!(matches!(
        Controller::new(
            &mut control,
            Policy {
                max_slew_time: Duration::MAX,
                ..policy()
            },
            [Source { priority: 0 }],
            InitialTime::Preserve
        ),
        Err(Error::InvalidPolicy)
    ));
    assert!(control.calls.is_empty());
    assert_eq!(control.pending, 8_000);
}
