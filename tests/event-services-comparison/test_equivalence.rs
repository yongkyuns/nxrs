#[path = "core.rs"]
mod protocol;
use protocol::*;
unsafe extern "C" {
    fn es_work_value(value: u32, token: u32, iterations: u32) -> u32;
    fn es_schedule_init(s: *mut Schedule, service: u32, profile: u32);
    fn es_schedule_due(s: *mut Schedule, now: u32, release: *mut Release) -> i32;
    fn es_make_event(e: *mut Event, source: u32, peer: u32, release: *const Release, posted: u32);
    fn es_handle(s: *mut State, event: *const Event, service: u32) -> i32;
    fn es_hist_add(h: *mut Histogram, value: u32);
    fn es_hist_percentile(h: *const Histogram, percent: u32) -> u32;
}
#[test]
fn c_and_rust_bounded_work_match_wrapping_values() {
    let values = [0, 1, 0x1234_5678, 0xdead_beef, u32::MAX - 1, u32::MAX];
    let tokens = [0, 0x7f4a_7c15, 0x9e37_79b9, 0x8000_0001, u32::MAX];
    for value in values {
        for token in tokens {
            for iterations in [0, 1_000, 10_000, 100_000, 400_000] {
                let rust_output = work_value(value, token, iterations);
                let c_output = unsafe { es_work_value(value, token, iterations) };
                assert_eq!(
                    rust_output, c_output,
                    "value={value:#010x}, token={token:#010x}, iterations={iterations}"
                );
                if iterations == 0 {
                    assert_eq!(rust_output, value);
                }
            }
        }
    }
}

#[test]
fn c_and_rust_schedules_packets_and_handlers_match() {
    for profile in 0..3 {
        for service in 0..20 {
            let mut rust = Schedule::new(service, profile);
            let mut c = Schedule::default();
            unsafe {
                es_schedule_init(&mut c, service, profile);
            }
            assert_eq!(rust, c);
            let mut rs = [State::default(); 3];
            let mut cs = rs;
            loop {
                let next = rust.due(ES_DURATION_US * ES_HZ);
                let mut cr = Release::default();
                let result = unsafe { es_schedule_due(&mut c, ES_DURATION_US * ES_HZ, &mut cr) };
                assert_eq!(next.is_some(), result == 1);
                assert_eq!(rust, c);
                let Some(release) = next else {
                    break;
                };
                assert_eq!(release, cr);
                for n in 0..release.count {
                    let release = Release {
                        sequence: release.sequence + n,
                        ..release
                    };
                    for peer in 0..3 {
                        let event =
                            Event::new(service, peer, &release, release.scheduled_cycles + 1);
                        let mut ce = Event::default();
                        unsafe {
                            es_make_event(
                                &mut ce,
                                service,
                                peer,
                                &release,
                                release.scheduled_cycles + 1,
                            );
                        }
                        assert_eq!(event, ce);
                        let p = peer as usize;
                        assert!(rs[p].handle(&event, event.destination.into()).is_ok());
                        assert_eq!(
                            unsafe { es_handle(&mut cs[p], &event, event.destination.into()) },
                            0
                        );
                        assert_eq!(rs[p], cs[p]);
                        let before = cs[p];
                        assert_eq!(
                            unsafe { es_handle(&mut cs[p], &event, event.destination.into()) },
                            -1
                        );
                        assert_eq!(cs[p], before);
                    }
                }
            }
        }
    }
}
#[test]
fn c_and_rust_histograms_match_extreme_values() {
    let mut r = Histogram::default();
    let mut c = r;
    for value in [
        0,
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        12,
        15,
        31,
        32,
        1000,
        20000,
        1000000,
        u32::MAX,
    ] {
        r.add(value);
        unsafe {
            es_hist_add(&mut c, value);
        }
        assert_eq!(r, c);
        for p in [0, 1, 50, 95, 99, 100, 101] {
            assert_eq!(r.percentile(p), unsafe { es_hist_percentile(&c, p) });
        }
    }
}
