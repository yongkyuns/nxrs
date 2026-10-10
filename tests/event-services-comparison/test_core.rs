#[path = "core.rs"]
mod core;

use core::{Event, Histogram, Release, Schedule, State, destination, token};
use std::mem::size_of;

const DURATION_CYCLES: u32 = 2_000_000 * 240;
const CYCLES_PER_MS: u32 = 240_000;

#[test]
fn c_abi_sizes_match_contract() {
    assert_eq!(core::ES_CAPACITY, 8);
    assert_eq!(core::ES_EVENT_BYTES, 64);
    assert_eq!(core::ES_DRAIN_US, 500_000);
    assert_eq!(size_of::<Event>(), 64);
    assert_eq!(size_of::<Release>(), 16);
    assert_eq!(size_of::<Schedule>(), 32);
    assert_eq!(size_of::<core::Flow>(), 16);
    assert_eq!(size_of::<State>(), 152);
    assert_eq!(size_of::<Histogram>(), 264);
}

#[test]
fn work_value_preserves_input_when_iterations_are_zero() {
    for value in [0, 1, 0x1234_5678, u32::MAX] {
        assert_eq!(core::work_value(value, 0xa5a5_5a5a, 0), value);
    }
}

#[test]
fn work_ranges_preserve_wrapping_indices() {
    for value in [0, 1, 0xdead_beef, u32::MAX] {
        for start in [0u32, 1, 10_000, u32::MAX - 31] {
            for count in [0u32, 1, 2, 3, 7, 16, 17, 31] {
                let mut expected = value;
                for i in start..start.checked_add(count).unwrap() {
                    expected = expected
                        .rotate_left(5)
                        .wrapping_mul(0x9e37_79b9)
                        .wrapping_add(0x8000_0001 ^ i.wrapping_mul(0x7f4a_7c15));
                }
                assert_eq!(
                    core::work_value_range(value, 0x8000_0001, start, count),
                    expected
                );
            }
        }
    }
}

#[test]
#[should_panic(expected = "bounded work range")]
fn work_range_rejects_index_overflow() {
    core::work_value_range(0, 0, u32::MAX, 1);
}

#[test]
fn chunked_work_preserves_every_iteration_and_final_value() {
    for seed in [0, 1, u32::MAX, 0xdead_beef] {
        for count in [0, 1, 10_001, 100_000, 400_000] {
            let mut value = seed;
            let mut start = 0;
            while start < count {
                let chunk = (count - start).min(10_000);
                value = core::work_value_range(value, 0xabcdef01, start, chunk);
                start += chunk;
            }
            assert_eq!(value, core::work_value(seed, 0xabcdef01, count));
        }
    }
}

#[test]
fn schedule_cardinality_matches_all_services_and_profiles() {
    for service in 0..20 {
        for profile in 0..3 {
            let mut schedule = Schedule::new(service, profile);
            let mut releases = [0u32; 3];
            let mut events = [0u32; 3];
            while let Some(release) = schedule.due(DURATION_CYCLES - 1) {
                assert!(release.scheduled_cycles < DURATION_CYCLES);
                releases[release.kind as usize] += 1;
                events[release.kind as usize] += release.count;
                assert_eq!(
                    release.sequence + release.count,
                    schedule.sequence[release.kind as usize]
                );
            }
            let phase = if profile == 2 { 0 } else { service };
            let control = count_deadlines(250 + phase, 250);
            let data_period = if profile == 2 { 10 } else { 50 };
            let data_phase = if profile == 2 { 0 } else { service + 2 };
            let data = count_deadlines(data_period + data_phase, data_period);
            let status_phase = if profile == 2 { 0 } else { service + 4 };
            let status = count_deadlines(100 + status_phase, 100);
            assert_eq!(
                releases,
                [control, data, status],
                "service={service}, profile={profile}"
            );
            let data_batch = match profile {
                1 => 2,
                2 => 16,
                _ => 1,
            };
            assert_eq!(
                events,
                [control, data * data_batch, status],
                "service={service}, profile={profile}"
            );
            assert_eq!(schedule.next_deadline(), DURATION_CYCLES);
        }
    }
}

fn count_deadlines(first_ms: u32, period_ms: u32) -> u32 {
    if first_ms * CYCLES_PER_MS >= DURATION_CYCLES {
        0
    } else {
        (DURATION_CYCLES - 1 - first_ms * CYCLES_PER_MS) / (period_ms * CYCLES_PER_MS) + 1
    }
}

#[test]
fn routing_and_event_encoding_match_contract() {
    for source in 0..20 {
        for peer in 0..3 {
            let release = Release {
                scheduled_cycles: 17,
                sequence: 9,
                count: 1,
                kind: 2,
            };
            let event = Event::new(source, peer, &release, 23);
            assert_eq!(event.destination as u32, destination(source, peer));
            assert_eq!(event.source as u32, source);
            assert_eq!(event.peer as u32, peer);
            assert_eq!(event.token, token(source, destination(source, peer), 2, 9));
            assert_eq!(event.scheduled_cycles, 17);
            assert_eq!(event.posted_cycles, 23);
            for i in 0..44 {
                assert_eq!(
                    event.payload[i],
                    ((event.token >> ((i % 4) * 8)) as u8) ^ i as u8
                );
            }
        }
    }
}

#[test]
fn burst_sequences_advance_by_batch_size_and_due_catches_up() {
    let mut schedule = Schedule::new(0, 1);
    let first = schedule.due(u32::MAX).unwrap();
    assert_eq!(first.kind, 1);
    assert_eq!(first.sequence, 0);
    assert_eq!(first.count, 2);
    assert_eq!(first.scheduled_cycles, 52 * CYCLES_PER_MS);
    let second = schedule.due(u32::MAX).unwrap();
    assert_eq!(second.kind, 1);
    assert_eq!(second.sequence, 2);
    assert_eq!(second.count, 2);
    assert_eq!(second.scheduled_cycles, 102 * CYCLES_PER_MS);
}

#[test]
fn no_release_at_or_after_duration_even_after_late_catchup() {
    let mut schedule = Schedule::new(19, 0);
    while let Some(release) = schedule.due(u32::MAX) {
        assert!(release.scheduled_cycles < DURATION_CYCLES);
    }
    assert_eq!(schedule.next_deadline(), DURATION_CYCLES);
    assert!(schedule.due(u32::MAX).is_none());
}

fn valid_event(sequence: u32) -> Event {
    let release = Release {
        scheduled_cycles: 0,
        sequence,
        count: 1,
        kind: 1,
    };
    Event::new(0, 0, &release, 0)
}

#[test]
fn handler_accepts_gaps_and_rejects_invalid_packets_without_mutation() {
    let mut state = State::default();
    assert_eq!(state.handle(&valid_event(2), 1), Ok(()));
    let before = state;

    let mut bad = valid_event(3);
    bad.payload[43] ^= 1;
    assert_eq!(state.handle(&bad, 1), Err(()));
    assert_eq!(state, before);

    let mut bad = valid_event(3);
    bad.token ^= 1;
    assert_eq!(state.handle(&bad, 1), Err(()));
    assert_eq!(state, before);

    let mut bad = valid_event(3);
    bad.destination = 2;
    assert_eq!(state.handle(&bad, 1), Err(()));
    assert_eq!(state, before);

    let mut bad = valid_event(3);
    bad.kind = 3;
    assert_eq!(state.handle(&bad, 1), Err(()));
    assert_eq!(state, before);

    assert_eq!(state.handle(&valid_event(7), 1), Ok(()));
    let flow = state.received[1];
    assert_eq!(flow.count, 2);
    assert_eq!(flow.last_sequence, 7);
    assert_eq!(flow.initialized, 1);
}

#[test]
fn handler_rejects_reordered_and_duplicate_sequences_without_mutation() {
    let mut state = State::default();
    assert_eq!(state.handle(&valid_event(4), 1), Ok(()));
    let before = state;
    assert_eq!(state.handle(&valid_event(4), 1), Err(()));
    assert_eq!(state, before);
    assert_eq!(state.handle(&valid_event(3), 1), Err(()));
    assert_eq!(state, before);
}

#[test]
fn token_digest_and_value_use_wrapping_arithmetic() {
    let mut state = State {
        value: u32::MAX - 2,
        ..State::default()
    };
    let event = valid_event(u32::MAX);
    assert_eq!(state.handle(&event, 1), Ok(()));
    let flow = state.received[1];
    assert_eq!(flow.digest, event.token);
    assert_eq!(flow.last_sequence, u32::MAX);
    assert_eq!(
        state.value,
        ((u32::MAX - 2).rotate_left(3) ^ event.token).wrapping_add(0x7f4a_7c15)
    );
    let next = Event::new(
        0,
        0,
        &Release {
            sequence: u32::MAX,
            kind: 1,
            ..Release::default()
        },
        0,
    );
    assert_eq!(next.token, event.token);
}

#[test]
fn histogram_uses_conservative_nearest_rank_bounds_capped_at_maximum() {
    let mut histogram = Histogram::default();
    for sample in [1, 2, 3, 4, 6, 8, 12, 13, 20, u32::MAX] {
        histogram.add(sample);
    }
    assert_eq!(histogram.percentile(1), 1);
    assert_eq!(histogram.percentile(50), 6);
    assert_eq!(histogram.percentile(90), 24);
    assert_eq!(histogram.percentile(100), u32::MAX);

    let mut capped = Histogram::default();
    capped.add(5);
    assert_eq!(capped.percentile(100), 5);
    assert_eq!(capped.percentile(150), 5);
    assert_eq!(Histogram::default().percentile(99), 0);
}
