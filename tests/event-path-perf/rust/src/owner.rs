//! P3 three-class owner. The ready-first strategy is an experimental dispatch
//! optimization, not a new transport or a remedy for reservation-order stalls.
#![forbid(unsafe_code)]

use crossbeam_channel::{bounded, never, Receiver, Sender, TryRecvError, TrySendError};
use std::{
    hint::black_box,
    sync::{atomic::{AtomicBool, Ordering}, Arc},
    thread,
    time::{Duration, Instant},
};

use crate::{native::{self, Gate, Result, Schedule}, payload::{Bytes16, Payload}, support};

const BURST: usize = 8;
const CAPACITY: usize = 8;
const DEADLINE: Duration = Duration::from_millis(10);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Strategy { Select, ReadyFirst }

#[derive(Debug, PartialEq, Eq)]
enum Turn<T> {
    Stop,
    Important(T),
    Ordinary(T),
    ImportantClosed,
    OrdinaryClosed,
    Deadline,
    Closed,
}

struct Owner<T> {
    stop: Receiver<()>,
    important: Receiver<T>,
    ordinary: Receiver<T>,
    pending: Option<Turn<T>>,
    important_open: bool,
    ordinary_open: bool,
    streak: usize,
    strategy: Strategy,
    select_calls: u32,
    ready_hits: u32,
    deadline_turns: u32,
}

type Endpoints<T> = (Owner<T>, Sender<()>, Sender<T>, Sender<T>);

fn channels<T>(strategy: Strategy) -> Endpoints<T> {
    let (stop_tx, stop) = bounded(1);
    let (important_tx, important) = bounded(CAPACITY);
    let (ordinary_tx, ordinary) = bounded(CAPACITY);
    (Owner {
        stop, important, ordinary, pending: None,
        important_open: true, ordinary_open: true, streak: 0, strategy,
        select_calls: 0, ready_hits: 0, deadline_turns: 0,
    }, stop_tx, important_tx, ordinary_tx)
}

impl<T> Owner<T> {
    fn try_data(&self, ordinary_first: bool) -> Option<Turn<T>> {
        fn attempt<T>(receiver: &Receiver<T>, ordinary: bool) -> Option<Turn<T>> {
            match receiver.try_recv() {
                Ok(value) => Some(if ordinary { Turn::Ordinary(value) } else { Turn::Important(value) }),
                Err(TryRecvError::Disconnected) => Some(if ordinary {
                    Turn::OrdinaryClosed
                } else { Turn::ImportantClosed }),
                Err(TryRecvError::Empty) => None,
            }
        }
        if ordinary_first {
            attempt(&self.ordinary, true).or_else(|| attempt(&self.important, false))
        } else {
            attempt(&self.important, false).or_else(|| attempt(&self.ordinary, true))
        }
    }

    fn select(&mut self, wait: Duration) -> Turn<T> {
        self.select_calls += 1;
        if self.streak >= BURST {
            crossbeam_channel::select_biased! {
                recv(self.stop) -> _ => Turn::Stop,
                recv(self.ordinary) -> value => match value {
                    Ok(value) => Turn::Ordinary(value), Err(_) => Turn::OrdinaryClosed,
                },
                recv(self.important) -> value => match value {
                    Ok(value) => Turn::Important(value), Err(_) => Turn::ImportantClosed,
                },
                default(wait) => Turn::Deadline,
            }
        } else {
            crossbeam_channel::select_biased! {
                recv(self.stop) -> _ => Turn::Stop,
                recv(self.important) -> value => match value {
                    Ok(value) => Turn::Important(value), Err(_) => Turn::ImportantClosed,
                },
                recv(self.ordinary) -> value => match value {
                    Ok(value) => Turn::Ordinary(value), Err(_) => Turn::OrdinaryClosed,
                },
                default(wait) => Turn::Deadline,
            }
        }
    }

    /// One dispatch turn. It is NOT a WCET-bounded channel operation. Ready-first
    /// still requires P1's applicable reservation-order scheduling contract.
    fn next(&mut self, deadline: Instant) -> Turn<T> {
        self.next_with(deadline, Instant::now)
    }

    fn next_with(&mut self, deadline: Instant, mut now: impl FnMut() -> Instant) -> Turn<T> {
        // Stop is checked before each dispatch, including a retained selection.
        // A stop that arrives after this check may leave one in-flight event.
        if !matches!(self.stop.try_recv(), Err(TryRecvError::Empty)) {
            return Turn::Stop;
        }
        let before = now();
        if before >= deadline {
            self.deadline_turns += 1;
            return Turn::Deadline;
        }
        if !self.important_open && !self.ordinary_open && self.pending.is_none() {
            return Turn::Closed;
        }
        let selected = if let Some(pending) = self.pending.take() {
            pending
        } else if self.strategy == Strategy::ReadyFirst {
            if let Some(ready) = self.try_data(self.streak >= BURST) {
                self.ready_hits += 1;
                ready
            } else {
                // The channel selector itself rechecks/registers. No separate
                // notification bit or home-grown check-then-sleep race exists.
                self.select(deadline.saturating_duration_since(before))
            }
        } else {
            self.select(deadline.saturating_duration_since(before))
        };
        // Waiting/selection can cross a deadline. Retain already-consumed data
        // while the caller performs deadline work; never lose a selected event.
        if !matches!(selected, Turn::Stop | Turn::Deadline) && now() >= deadline {
            self.pending = Some(selected);
            self.deadline_turns += 1;
            return Turn::Deadline;
        }
        match &selected {
            Turn::Important(_) => self.streak = (self.streak + 1).min(BURST),
            Turn::Ordinary(_) => self.streak = 0,
            Turn::ImportantClosed => {
                self.important = never();
                self.important_open = false;
            }
            Turn::OrdinaryClosed => {
                self.ordinary = never();
                self.ordinary_open = false;
            }
            Turn::Deadline => self.deadline_turns += 1,
            _ => {}
        }
        selected
    }
}

#[derive(Clone, Copy)]
struct Case { strategy: Strategy, providers: usize, periodic: bool, backlog: bool }

impl Case {
    fn parse(value: &str) -> Result<Self> {
        let (strategy, tail) = if let Some(tail) = value.strip_prefix("owner-select-") {
            (Strategy::Select, tail)
        } else if let Some(tail) = value.strip_prefix("owner-ready-") {
            (Strategy::ReadyFirst, tail)
        } else { return Err("unknown-owner-case"); };
        let (providers, periodic, backlog) = match tail {
            "burst-1" => (1, false, false), "burst-4" => (4, false, false),
            "periodic-1" => (1, true, false), "periodic-4" => (4, true, false),
            "backlog" => (0, false, true),
            _ => return Err("unknown-owner-case"),
        };
        Ok(Self { strategy, providers, periodic, backlog })
    }
}

struct Attempt { value: Bytes16, start: u32, end: u32, begin_ns: u64, end_ns: u64, admitted: bool }
struct Delivery { value: Bytes16, begin: u32, end: u32, end_ns: u64, discarded: bool }
struct Producer { tid: i32, schedule: Schedule, attempts: Vec<Attempt> }
struct OwnerResult {
    tid: i32, schedule: Schedule, deliveries: Vec<Delivery>, select_calls: u32,
    ready_hits: u32, deadlines: u32, stop_tick: u32, drain_ticks: Option<u64>,
}

fn handler(value: Bytes16) {
    // Fixed bounded synthetic handler; black_box prevents dead-code elimination.
    let mut work = value.checksum();
    for _ in 0..16 { work = black_box(work.rotate_left(3).wrapping_add(7)); }
    black_box(work);
}

fn delivery(value: Bytes16, discarded: bool) -> Result<Delivery> {
    if !value.valid(value.sequence(), value.provider()) { return Err("owner-payload"); }
    let begin = native::counter();
    if !discarded { handler(value); }
    let end = native::counter();
    Ok(Delivery { value, begin, end, end_ns: native::monotonic_ns()?, discarded })
}

fn take_value(turn: Turn<Bytes16>) -> Option<Bytes16> {
    match turn { Turn::Important(value) | Turn::Ordinary(value) => Some(value), _ => None }
}

fn owner_worker(mut owner: Owner<Bytes16>, start: Arc<Gate>, cancelled: Arc<AtomicBool>, limit: usize) -> Result<OwnerResult> {
    let schedule = support::configure(180)?;
    let tid = native::tid();
    let mut deliveries = Vec::with_capacity(limit);
    start.wait(support::WAIT_MS)?;
    let mut deadline = Instant::now() + DEADLINE;
    let stop_tick;
    loop {
        if cancelled.load(Ordering::Acquire) { return Err("owner-cancelled"); }
        match owner.next(deadline) {
            Turn::Stop => { stop_tick = native::counter(); break; }
            Turn::Deadline => { deadline = Instant::now() + DEADLINE; }
            Turn::Important(value) | Turn::Ordinary(value) => {
                if deliveries.len() >= limit { return Err("owner-delivery-overflow"); }
                deliveries.push(delivery(value, false)?);
            }
            Turn::ImportantClosed | Turn::OrdinaryClosed => {}
            Turn::Closed => return Err("owner-closed-before-stop"),
        }
    }
    // Stop policy: cancel queued data, count every discard, and do no handler
    // work after observing stop. Caller posts stop only after all providers join.
    if let Some(value) = owner.pending.take().and_then(take_value) {
        deliveries.push(delivery(value, true)?);
    }
    for receiver in [&owner.important, &owner.ordinary] {
        while let Ok(value) = receiver.try_recv() {
            if deliveries.len() >= limit { return Err("owner-discard-overflow"); }
            deliveries.push(delivery(value, true)?);
        }
    }
    Ok(OwnerResult { tid, schedule, deliveries, select_calls: owner.select_calls,
        ready_hits: owner.ready_hits, deadlines: owner.deadline_turns, stop_tick, drain_ticks: None })
}

fn produce(provider: u32, count: u32, periodic: bool, important: Sender<Bytes16>, ordinary: Sender<Bytes16>, start: Arc<Gate>, cancelled: Arc<AtomicBool>) -> Result<Producer> {
    let schedule = support::configure(180)?;
    let tid = native::tid();
    let mut attempts = Vec::with_capacity(count as usize);
    start.wait(support::WAIT_MS)?;
    let epoch = Instant::now();
    for sequence in 1..=count {
        if cancelled.load(Ordering::Acquire) { return Err("producer-cancelled"); }
        if periodic {
            let planned = epoch + Duration::from_millis(u64::from(sequence - 1));
            if let Some(wait) = planned.checked_duration_since(Instant::now()) { thread::sleep(wait); }
        } else if sequence > 1 && (sequence - 1) % 16 == 0 {
            // Finite FIFO runnable burst, followed by a real blocking point.
            thread::sleep(Duration::from_millis(1));
        }
        let value = Bytes16::new(sequence, provider);
        let sender = if sequence % 9 == 0 { &important } else { &ordinary };
        let begin_ns = native::monotonic_ns()?;
        let begin = native::counter();
        let admitted = match sender.try_send(value) {
            Ok(()) => true,
            Err(TrySendError::Full(returned)) if returned == value => false,
            Err(_) => return Err("owner-admission-closed-or-mutated"),
        };
        let end = native::counter();
        attempts.push(Attempt { value, start: begin, end, begin_ns, end_ns: native::monotonic_ns()?, admitted });
    }
    Ok(Producer { tid, schedule, attempts })
}

fn concurrent(case: Case, count: u32) -> Result<(Vec<Producer>, OwnerResult, u32)> {
    let (owner, stop, important, ordinary) = channels(case.strategy);
    let start = Gate::new()?;
    let cancelled = Arc::new(AtomicBool::new(false));
    let owner_start = start.clone();
    let owner_cancel = cancelled.clone();
    let owner_handle = thread::Builder::new().stack_size(support::WORKER_STACK).spawn(move || {
        owner_worker(owner, owner_start, owner_cancel, count as usize * case.providers)
    }).map_err(|_| "owner-spawn")?;
    let mut handles = Vec::with_capacity(case.providers);
    let mut failure = None;
    for index in 0..case.providers {
        let (a, b, gate, cancel) = (important.clone(), ordinary.clone(), start.clone(), cancelled.clone());
        match thread::Builder::new().stack_size(support::WORKER_STACK).spawn(move || {
            produce(index as u32, count, case.periodic, a, b, gate, cancel)
        }) {
            Ok(handle) => handles.push(handle),
            Err(_) => { failure = Some("provider-spawn"); cancelled.store(true, Ordering::Release); break; }
        }
    }
    // Release all successfully spawned workers, including partial-start failure.
    for _ in 0..=handles.len() { if start.post().is_err() { crate::fatal("owner-start-gate"); } }
    let mut producers = Vec::with_capacity(handles.len());
    for handle in handles {
        match support::join_bounded(handle) {
            Ok(producer) => producers.push(producer),
            Err(error) => { failure.get_or_insert(error); cancelled.store(true, Ordering::Release); }
        }
    }
    let stop_tick = native::counter();
    if stop.try_send(()).is_err() { failure.get_or_insert("owner-stop-admission"); }
    let joined = support::join_bounded(owner_handle);
    // Keep data endpoints connected until owner has observed dedicated stop.
    drop((important, ordinary, stop));
    if let Some(error) = failure { return Err(error); }
    Ok((producers, joined?, stop_tick))
}

fn backlog(case: Case, rounds: u32) -> Result<(Vec<Producer>, OwnerResult, u32)> {
    let (mut owner, stop, important, ordinary) = channels(case.strategy);
    let schedule = support::configure(180)?;
    let tid = native::tid();
    let mut deliveries = Vec::with_capacity(rounds as usize * CAPACITY * 2);
    let mut attempts = Vec::with_capacity(deliveries.capacity());
    let mut sequence = 0;
    let clock = support::Clock::new()?;
    let mut drain_ticks = 0_u64;
    for _ in 0..rounds {
        for sender in [&important, &ordinary] {
            for _ in 0..CAPACITY {
                sequence += 1;
                let value = Bytes16::new(sequence, 0);
                let begin_ns = native::monotonic_ns()?;
                let begin = native::counter();
                sender.try_send(value).map_err(|_| "backlog-admission")?;
                attempts.push(Attempt { value, start: begin, end: native::counter(),
                    begin_ns, end_ns: native::monotonic_ns()?, admitted: true });
            }
        }
        let drain_begin_ns = native::monotonic_ns()?;
        let drain_begin = native::counter();
        for _ in 0..CAPACITY * 2 {
            let value = take_value(owner.next(Instant::now() + Duration::from_secs(1)))
                .ok_or("backlog-owner-turn")?;
            deliveries.push(delivery(value, false)?);
        }
        let delta = native::counter().wrapping_sub(drain_begin);
        clock.window(drain_begin_ns, native::monotonic_ns()?, &[delta])?;
        drain_ticks += u64::from(delta);
    }
    let stop_tick = native::counter();
    stop.try_send(()).map_err(|_| "backlog-stop-admission")?;
    if owner.next(Instant::now() + DEADLINE) != Turn::Stop { return Err("backlog-stop"); }
    let observed_stop = native::counter();
    Ok((vec![Producer { tid, schedule, attempts }], OwnerResult {
        tid, schedule, deliveries, select_calls: owner.select_calls, ready_hits: owner.ready_hits,
        deadlines: owner.deadline_turns, stop_tick: observed_stop, drain_ticks: Some(drain_ticks),
    }, stop_tick))
}

pub fn execute(name: &str, samples: u32, warmup: u32, payload: &str) -> Result<()> {
    let case = Case::parse(name)?;
    if !(1..=256).contains(&samples) || warmup != 0 || payload != "bytes16" {
        return Err("owner-requires-1-to-256-samples-zero-warmup-bytes16");
    }
    let clock = support::Clock::new()?;
    let original = native::schedule(native::tid())?;
    let before = native::monotonic_ns()?;
    let watchdog = support::Watchdog::start()?;
    let outcome = support::configure(220).and_then(|_| {
        if case.backlog { backlog(case, samples) } else { concurrent(case, samples) }
    });
    let restored = native::set_schedule(native::tid(), original);
    let stopped = watchdog.finish();
    restored?;
    stopped?;
    let (producers, owner, stop_tick) = outcome?;
    let elapsed = native::monotonic_ns()?.checked_sub(before).ok_or("owner-clock-reversed")?;
    let total = producers.iter().map(|p| p.attempts.len()).sum::<usize>();
    let mut seen = vec![false; owner.deliveries.len()];
    let mut admitted = 0;
    let mut handled = 0;
    let mut discarded = 0;
    println!(concat!("OWNER_PATH_PERF {{\"schema\":\"owner-path-v1\",\"record\":\"experiment\",",
        "\"case\":\"{}\",\"build_id\":\"{}\",\"runtime\":\"{}\",\"samples\":{},",
        "\"clock_hz\":{},\"counter_bits\":32,\"capacity\":8,\"stop_capacity\":1,\"important_burst\":8,",
        "\"payload_bytes\":16,\"worker_stack\":{},\"allocation_policy\":\"preallocated-observation-vectors\",",
        "\"physical_timing_qualified\":false,\"priority_progress_qualified\":false}}"),
        name, env!("NXRS_EVENT_PATH_BUILD_ID"), if native::is_nuttx() { "nuttx-unqualified" } else { "host-functional-only" },
        samples, clock.hz, support::WORKER_STACK);
    for (index, producer) in producers.iter().enumerate() {
        if native::is_nuttx() && (producer.schedule.policy != 1 || producer.schedule.priority != 180) {
            return Err("owner-provider-schedule");
        }
        println!("OWNER_PATH_PERF {{\"schema\":\"owner-path-v1\",\"record\":\"provider\",\"provider\":{index},\"tid\":{},\"policy\":{},\"priority\":{}}}", producer.tid, producer.schedule.policy, producer.schedule.priority);
        for attempt in &producer.attempts {
            let found = owner.deliveries.iter().enumerate().find(|(_, item)| item.value == attempt.value);
            if found.is_some() != attempt.admitted { return Err("owner-conservation"); }
            let mut handler_start = String::from("null");
            let mut handler_end = String::from("null");
            let mut was_discarded = false;
            let mut end_ns = attempt.end_ns;
            let mut deltas = vec![attempt.end.wrapping_sub(attempt.start)];
            if let Some((position, item)) = found {
                if seen[position] { return Err("owner-duplicate-delivery"); }
                seen[position] = true;
                admitted += 1;
                was_discarded = item.discarded;
                end_ns = end_ns.max(item.end_ns);
                if was_discarded { discarded += 1; } else {
                    handled += 1;
                    handler_start = item.begin.to_string();
                    handler_end = item.end.to_string();
                    deltas.extend([item.begin.wrapping_sub(attempt.start), item.end.wrapping_sub(attempt.start)]);
                }
            }
            let upper_ns = clock.window(attempt.begin_ns, end_ns, &deltas)?;
            println!(concat!("OWNER_PATH_PERF {{\"schema\":\"owner-path-v1\",\"record\":\"attempt\",\"provider\":{},",
                "\"sequence\":{},\"checksum\":{},\"start_ticks\":{},\"admission_return_ticks\":{},\"accepted\":{},",
                "\"discarded\":{},\"handler_start_ticks\":{},\"handler_end_ticks\":{},\"elapsed_upper_ns\":{}}}"),
                attempt.value.provider(), attempt.value.sequence(), attempt.value.checksum(), attempt.start, attempt.end,
                attempt.admitted, was_discarded, handler_start, handler_end, upper_ns);
        }
    }
    if seen.iter().any(|value| !value) { return Err("owner-unaccounted-delivery"); }
    if native::is_nuttx() && (owner.schedule.policy != 1 || owner.schedule.priority != 180) {
        return Err("owner-schedule");
    }
    println!(concat!("OWNER_PATH_PERF {{\"schema\":\"owner-path-v1\",\"record\":\"result\",\"success\":true,",
        "\"attempted\":{},\"admitted\":{},\"rejected\":{},\"handled\":{},\"discarded\":{},",
        "\"select_calls\":{},\"ready_hits\":{},\"deadline_turns\":{},\"stop_request_ticks\":{},\"stop_observed_ticks\":{},",
        "\"drain_ticks\":{},\"owner_tid\":{},\"owner_policy\":{},\"owner_priority\":{},\"elapsed_ns\":{},\"threads_joined\":true,",
        "\"actual_switch_count\":null,\"cpu_time_ns\":null,\"physical_timing_qualified\":false,\"priority_progress_qualified\":false}}"),
        total, admitted, total - admitted, handled, discarded, owner.select_calls, owner.ready_hits, owner.deadlines,
        stop_tick, owner.stop_tick, owner.drain_ticks.map(|v| v.to_string()).unwrap_or_else(|| "null".into()), owner.tid, owner.schedule.policy, owner.schedule.priority, elapsed);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn future() -> Instant { Instant::now() + Duration::from_secs(2) }

    #[test]
    fn both_strategies_preserve_priority_fairness_and_isolated_stop() {
        for strategy in [Strategy::Select, Strategy::ReadyFirst] {
            let (mut owner, stop, important, ordinary) = channels(strategy);
            for n in 0..8 { important.try_send(n).unwrap(); ordinary.try_send(n).unwrap(); }
            assert!(matches!(ordinary.try_send(42), Err(TrySendError::Full(42))));
            for n in 0..8 {
                assert_eq!(owner.next(future()), Turn::Important(n));
                important.try_send(n + 8).unwrap();
            }
            assert_eq!(owner.next(future()), Turn::Ordinary(0));
            stop.try_send(()).unwrap();
            assert_eq!(owner.next(future()), Turn::Stop);
            assert_eq!(ordinary.len(), 7);
        }
    }

    #[test]
    fn deadline_before_and_after_selection_never_loses_the_message() {
        for strategy in [Strategy::Select, Strategy::ReadyFirst] {
            let (mut owner, _stop, important, _ordinary) = channels(strategy);
            important.try_send(7).unwrap();
            let now = Instant::now();
            assert_eq!(owner.next_with(now, || now), Turn::Deadline);
            assert_eq!(important.len(), 1);
            let deadline = now + Duration::from_millis(1);
            let mut stamps = [now, deadline].into_iter();
            assert_eq!(owner.next_with(deadline, || stamps.next().unwrap()), Turn::Deadline);
            assert_eq!(important.len(), 0);
            assert_eq!(owner.next(future()), Turn::Important(7));
            assert!(owner.pending.is_none());
        }
    }

    #[test]
    fn closed_arms_are_disabled_and_all_closed_terminates_without_spin() {
        for strategy in [Strategy::Select, Strategy::ReadyFirst] {
            let (mut owner, _stop, important, ordinary) = channels::<u32>(strategy);
            drop(important);
            assert_eq!(owner.next(future()), Turn::ImportantClosed);
            ordinary.try_send(1).unwrap();
            assert_eq!(owner.next(future()), Turn::Ordinary(1));
            drop(ordinary);
            assert_eq!(owner.next(future()), Turn::OrdinaryClosed);
            assert_eq!(owner.next(future()), Turn::Closed);
        }
    }

    #[test]
    fn ready_first_removes_selector_calls_for_the_same_checked_backlog() {
        for strategy in [Strategy::Select, Strategy::ReadyFirst] {
            let (mut owner, _stop, important, ordinary) = channels(strategy);
            for n in 0..8 { important.try_send(n).unwrap(); ordinary.try_send(n).unwrap(); }
            let actual: Vec<_> = (0..16).map(|_| owner.next(future())).collect();
            let expected: Vec<_> = (0..8).map(Turn::Important).chain((0..8).map(Turn::Ordinary)).collect();
            assert_eq!(actual, expected);
            assert_eq!(owner.select_calls, if strategy == Strategy::Select { 16 } else { 0 });
            assert_eq!(owner.ready_hits, if strategy == Strategy::ReadyFirst { 16 } else { 0 });
        }
    }

    #[test]
    fn empty_wait_is_rearmed_and_woken_by_each_class() {
        for strategy in [Strategy::Select, Strategy::ReadyFirst] {
            for important_arm in [false, true] {
                let (mut owner, stop, important, ordinary) = channels(strategy);
                let worker = thread::spawn(move || owner.next(future()));
                if important_arm { important.send(7).unwrap(); } else { ordinary.send(7).unwrap(); }
                assert_eq!(worker.join().unwrap(), if important_arm { Turn::Important(7) } else { Turn::Ordinary(7) });
                drop(stop);
            }
        }
    }

    #[test]
    fn stop_survives_a_pending_selected_value() {
        let (mut owner, stop, important, _ordinary) = channels(Strategy::ReadyFirst);
        important.send(9).unwrap();
        let now = Instant::now();
        let deadline = now + Duration::from_millis(1);
        let mut stamps = [now, deadline].into_iter();
        assert_eq!(owner.next_with(deadline, || stamps.next().unwrap()), Turn::Deadline);
        stop.send(()).unwrap();
        assert_eq!(owner.next(future()), Turn::Stop);
        assert_eq!(owner.pending.take().and_then(take_value_generic), Some(9));
    }

    fn take_value_generic<T>(turn: Turn<T>) -> Option<T> {
        match turn { Turn::Important(value) | Turn::Ordinary(value) => Some(value), _ => None }
    }

    #[test]
    fn parameters_refuse_unbounded_or_relabelled_owner_cases() {
        assert!(Case::parse("owner-ready-burst-4").is_ok());
        assert!(Case::parse("owner-ready-burst-2").is_err());
        assert!(execute("owner-select-backlog", 0, 0, "bytes16").is_err());
        assert!(execute("owner-select-backlog", 16, 1, "bytes16").is_err());
        assert!(execute("owner-select-backlog", 16, 0, "imu16").is_err());
    }
}
