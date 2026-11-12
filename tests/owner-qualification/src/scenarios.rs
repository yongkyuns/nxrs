use crate::{platform, Outcome};
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicUsize, Ordering::SeqCst};
use std::sync::{mpsc, Arc};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use wifi::{
    AccessPoint, Capabilities, Command, Completion, Controller, CredentialStorage, DeliveryError,
    Error, Event, EventKind, LinkState, OperationId, ProviderStatus, Rejected, Request, Security,
    SessionId, Update, Wifi,
};
use wifi_scan_owner::{channels, OwnerError, OwnerReport};

const DEADLINE: Duration = Duration::from_secs(4);
const WORKER_STACK: usize = if cfg!(target_os = "nuttx") {
    16_384
} else {
    131_072
};

fn until(mut predicate: impl FnMut() -> bool) {
    let end = Instant::now() + DEADLINE;
    while !predicate() {
        assert!(Instant::now() < end, "qualification phase deadline");
        thread::sleep(Duration::from_millis(1));
    }
}

fn spawn<T: Send + 'static>(
    role: &'static str,
    priority: i32,
    body: impl FnOnce(i32) -> T + Send + 'static,
) -> JoinHandle<(platform::Participant, T)> {
    thread::Builder::new()
        .name(role.into())
        .stack_size(WORKER_STACK)
        .spawn(move || {
            let participant = platform::enter(role, priority);
            let value = body(participant.start.tid.unwrap_or(0));
            (platform::leave(participant), value)
        })
        .expect("ordinary std thread creation")
}

fn join<T>(handle: JoinHandle<(platform::Participant, T)>, outcome: &mut Outcome) -> T {
    let (participant, value) = handle.join().expect("worker panic");
    outcome.participants.push(participant);
    outcome.joined += 1;
    value
}

fn observe_wait(
    tid: &AtomicI32,
    priority: i32,
    mut phase: impl FnMut() -> bool,
    outcome: &mut Outcome,
) {
    outcome.required_waits += 1;
    until(|| phase() && (!platform::NATIVE || platform::waiting(tid.load(SeqCst), priority)));
    if platform::NATIVE {
        outcome.native_waits += 1;
    }
}

pub fn run(case: &str) -> Outcome {
    match case {
        "std-blocked-delivery" => std_blocked(),
        "std-full-progress" => std_full(),
        "std-disconnect" => std_disconnect(),
        "owner-independent-inputs" => independent_inputs(),
        "owner-full-stop" => full_stop(),
        "owner-fairness-deadline" => fairness_deadline(),
        "owner-failure-recovery" => failure_recovery(),
        _ => unreachable!(),
    }
}

fn std_blocked() -> Outcome {
    const COUNT: usize = 16;
    let mut outcome = Outcome::default();
    let (sender, receiver) = messaging::bounded::<[u32; 4]>(1);
    let phase = Arc::new(AtomicUsize::new(0));
    let release = Arc::new(AtomicUsize::new(0));
    let received = Arc::new(AtomicUsize::new(0));
    let tid = Arc::new(AtomicI32::new(0));
    let receive = spawn("std-receiver", 180, {
        let (phase, received, tid) = (phase.clone(), received.clone(), tid.clone());
        move |id| {
            tid.store(id, SeqCst);
            // Only this inbox wait follows each marker; setup/identity precede it.
            for sequence in 1..=COUNT {
                phase.store(sequence, SeqCst);
                assert_eq!(receiver.wait(Some(DEADLINE)).unwrap(), payload(sequence));
                received.store(sequence, SeqCst);
            }
            assert!(matches!(
                receiver.wait(Some(DEADLINE)),
                Err(mpsc::RecvTimeoutError::Disconnected)
            ));
        }
    });
    let send = spawn("std-producer", 120, {
        let release = release.clone();
        move |_| {
            for sequence in 1..=COUNT {
                until(|| release.load(SeqCst) == sequence);
                sender.try_send(payload(sequence)).unwrap();
            }
            drop(sender);
        }
    });
    for sequence in 1..=COUNT {
        observe_wait(
            &tid,
            180,
            || phase.load(SeqCst) == sequence && received.load(SeqCst) < sequence,
            &mut outcome,
        );
        release.store(sequence, SeqCst);
        until(|| received.load(SeqCst) == sequence);
    }
    join(send, &mut outcome);
    join(receive, &mut outcome);
    outcome.attempted = COUNT;
    outcome.admitted = COUNT;
    outcome.handled = COUNT;
    outcome.endpoints_closed = true;
    outcome
}

fn payload(sequence: usize) -> [u32; 4] {
    [
        sequence as u32,
        !(sequence as u32),
        0x51ed270b,
        (sequence as u32) ^ 0xa5a5a5a5,
    ]
}

fn std_full() -> Outcome {
    let mut outcome = Outcome::default();
    let (sender, receiver) = messaging::bounded(1);
    let attempted = Arc::new(AtomicBool::new(false));
    let release = Arc::new(AtomicBool::new(false));
    let drained = Arc::new(AtomicBool::new(false));
    let send = spawn("std-producer", 180, {
        let (attempted, drained) = (attempted.clone(), drained.clone());
        move |_| {
            sender.try_send(payload(1)).unwrap();
            assert_eq!(
                sender.try_send(payload(2)),
                Err(mpsc::TrySendError::Full(payload(2)))
            );
            attempted.store(true, SeqCst);
            // A bounded sleep lets the lower-priority consumer run; no busy retry.
            until(|| drained.load(SeqCst));
            sender.try_send(payload(2)).unwrap();
        }
    });
    let receive = spawn("std-receiver", 120, {
        let release = release.clone();
        move |_| {
            until(|| release.load(SeqCst));
            assert_eq!(receiver.wait(Some(DEADLINE)).unwrap(), payload(1));
            drained.store(true, SeqCst);
            assert_eq!(receiver.wait(Some(DEADLINE)).unwrap(), payload(2));
            assert!(matches!(
                receiver.wait(Some(DEADLINE)),
                Err(mpsc::RecvTimeoutError::Disconnected)
            ));
        }
    });
    until(|| attempted.load(SeqCst));
    release.store(true, SeqCst);
    join(send, &mut outcome);
    join(receive, &mut outcome);
    outcome.attempted = 3;
    outcome.admitted = 2;
    outcome.rejected = 1;
    outcome.handled = 2;
    outcome.endpoints_closed = true;
    outcome
}

fn std_disconnect() -> Outcome {
    let mut outcome = Outcome::default();
    let (sender, receiver) = messaging::bounded::<[u32; 4]>(1);
    let entered = Arc::new(AtomicBool::new(false));
    let tid = Arc::new(AtomicI32::new(0));
    let receive = spawn("std-receiver", 180, {
        let (entered, tid) = (entered.clone(), tid.clone());
        move |id| {
            tid.store(id, SeqCst);
            entered.store(true, SeqCst);
            assert!(matches!(
                receiver.wait(Some(DEADLINE)),
                Err(mpsc::RecvTimeoutError::Disconnected)
            ));
        }
    });
    observe_wait(&tid, 180, || entered.load(SeqCst), &mut outcome);
    drop(sender);
    join(receive, &mut outcome);
    outcome.endpoints_closed = true;
    outcome
}

#[derive(Default)]
struct State {
    stopped: AtomicBool,
    fault: AtomicBool,
    stop_failures: AtomicUsize,
    finishes: AtomicUsize,
}

struct FixtureWifi(Arc<State>);
impl Wifi for FixtureWifi {
    fn session(&self) -> SessionId {
        SessionId::new(7).unwrap()
    }
    fn capabilities(&self) -> Capabilities {
        Capabilities {
            scan: true,
            open_network: true,
            wpa2_personal: true,
            credential_storage: CredentialStorage::Volatile,
        }
    }
    fn status(&self) -> ProviderStatus {
        if self.0.stopped.load(SeqCst) {
            ProviderStatus::Stopped
        } else if self.0.fault.load(SeqCst) {
            ProviderStatus::Failed(Error::Io)
        } else {
            ProviderStatus::Ready
        }
    }
    fn fault(&self) -> Option<Error> {
        self.0.fault.load(SeqCst).then_some(Error::Io)
    }
    fn submit(&mut self, _: Request) -> Result<(), Rejected<Command, Error>> {
        Ok(())
    }
    fn request_stop(&mut self) -> Result<(), Error> {
        if self
            .0
            .stop_failures
            .compare_exchange(1, 0, SeqCst, SeqCst)
            .is_ok()
        {
            return Err(Error::Io);
        }
        self.0.stopped.store(true, SeqCst);
        Ok(())
    }
    fn finish(&mut self) -> Result<(), Error> {
        assert!(self.0.stopped.load(SeqCst));
        self.0.finishes.fetch_add(1, SeqCst);
        Ok(())
    }
}

fn event(kind: EventKind) -> Event {
    Event {
        session: SessionId::new(7).unwrap(),
        kind,
    }
}
fn result_event() -> Event {
    event(EventKind::ScanResult {
        operation: OperationId::new(1).unwrap(),
        access_point: AccessPoint {
            ssid: None,
            security: Security::Other,
            rssi_dbm: Some(-42),
        },
    })
}
fn completion(reported: u32) -> Event {
    event(EventKind::Completed {
        operation: OperationId::new(1).unwrap(),
        result: Ok(Completion::Scan {
            reported,
            truncated: false,
        }),
    })
}

fn independent_inputs() -> Outcome {
    let mut outcome = Outcome::default();
    let state = Arc::new(State::default());
    let (mut owner, stop, commands, events) = channels(2, 2).unwrap();
    let mut controller = Controller::new(FixtureWifi(state.clone()));
    controller.submit(Command::Scan, 0, 10_000).unwrap();
    let tid = Arc::new(AtomicI32::new(0));
    let clock_calls = Arc::new(AtomicUsize::new(0));
    let observations = Arc::new(AtomicUsize::new(0));
    let release = Arc::new(AtomicUsize::new(0));
    let provider_one = spawn("important-provider", 180, {
        let mut events = events.clone();
        let release = release.clone();
        move |_| {
            until(|| release.load(SeqCst) >= 1);
            events
                .try_emit(event(EventKind::LinkChanged(LinkState::Associated)))
                .unwrap();
        }
    });
    let provider_two = spawn("ordinary-provider", 180, {
        let mut events = events.clone();
        let release = release.clone();
        move |_| {
            until(|| release.load(SeqCst) >= 2);
            events.try_emit(result_event()).unwrap();
            until(|| release.load(SeqCst) >= 3);
            events.try_emit(completion(1)).unwrap();
        }
    });
    let worker = spawn("selected-owner", 180, {
        let (tid, clock_calls, observations) =
            (tid.clone(), clock_calls.clone(), observations.clone());
        move |id| {
            tid.store(id, SeqCst);
            let started = Instant::now();
            owner
                .run(
                    &mut controller,
                    &mut |report| {
                        match report {
                            OwnerReport::State {
                                link: LinkState::Associated,
                                ..
                            } => {
                                observations.store(1, SeqCst);
                            }
                            OwnerReport::Update(Update::ScanResult(value)) => {
                                assert_eq!(value.rssi_dbm, Some(-42));
                                observations.store(2, SeqCst);
                            }
                            OwnerReport::Update(Update::Finished(value)) => {
                                assert_eq!(
                                    value.result,
                                    Ok(Completion::Scan {
                                        reported: 1,
                                        truncated: false
                                    })
                                );
                                observations.store(3, SeqCst);
                                stop.request_stop().unwrap();
                            }
                            _ => {}
                        }
                        Ok(())
                    },
                    || {
                        clock_calls.fetch_add(1, SeqCst);
                        started.elapsed().as_millis() as u64
                    },
                )
                .unwrap();
            assert_eq!(controller.status(), ProviderStatus::Stopped);
            controller
        }
    });
    for sequence in 1..=3 {
        observe_wait(
            &tid,
            180,
            || observations.load(SeqCst) == sequence - 1 && clock_calls.load(SeqCst) % 2 == 1,
            &mut outcome,
        );
        release.store(sequence, SeqCst);
        until(|| observations.load(SeqCst) >= sequence);
    }
    // Composition joins source workers before provider reclamation. Stopped
    // alone is deliberately not treated as proof of callback completion.
    join(provider_one, &mut outcome);
    join(provider_two, &mut outcome);
    let mut controller = join(worker, &mut outcome);
    assert_eq!(state.finishes.load(SeqCst), 0);
    controller.finish().unwrap();
    assert_eq!(state.finishes.load(SeqCst), 1);
    assert_eq!(
        commands.try_submit(Command::Scan, 1).unwrap_err().error,
        DeliveryError::Closed
    );
    let mut events = events;
    assert_eq!(
        events.try_emit(result_event()).unwrap_err().error,
        DeliveryError::Closed
    );
    outcome.attempted = 3;
    outcome.admitted = 3;
    outcome.handled = 3;
    outcome.important_observations = 1;
    outcome.ordinary_observations = 2;
    outcome.stop_requests = 1;
    outcome.endpoints_closed = true;
    outcome
}

fn full_stop() -> Outcome {
    let mut outcome = Outcome::default();
    let state = Arc::new(State::default());
    let (mut owner, stop, commands, mut events) = channels(1, 1).unwrap();
    events
        .try_emit(event(EventKind::LinkChanged(LinkState::Associated)))
        .unwrap();
    events.try_emit(result_event()).unwrap();
    assert_eq!(
        events.try_emit(result_event()).unwrap_err().error,
        DeliveryError::Full
    );
    assert_eq!(
        commands.try_submit(Command::Scan, 1).unwrap_err().error,
        DeliveryError::Full
    );
    stop.request_stop().unwrap();
    assert_eq!(
        stop.request_stop(),
        Err(wifi_scan_owner::StopAdmissionError::AlreadyPending)
    );
    let worker = spawn("selected-owner", 180, {
        let state = state.clone();
        move |_| {
            let mut controller = Controller::new(FixtureWifi(state));
            let mut handled = 0;
            owner
                .run(
                    &mut controller,
                    &mut |report| {
                        if matches!(
                            report,
                            OwnerReport::State {
                                link: LinkState::Associated,
                                ..
                            } | OwnerReport::Update(Update::ScanResult(_))
                        ) {
                            handled += 1;
                        }
                        Ok(())
                    },
                    || 0,
                )
                .unwrap();
            assert_eq!(handled, 0);
            controller.finish().unwrap();
        }
    });
    join(worker, &mut outcome);
    assert_eq!(state.finishes.load(SeqCst), 1);
    assert_eq!(
        events.try_emit(result_event()).unwrap_err().error,
        DeliveryError::Closed
    );
    assert_eq!(
        commands.try_submit(Command::Scan, 1).unwrap_err().error,
        DeliveryError::Closed
    );
    outcome.attempted = 4;
    outcome.admitted = 2;
    outcome.rejected = 2;
    outcome.discarded = 2; // The actual owner retires both still-full receivers.
    outcome.stop_requests = 1;
    outcome.endpoints_closed = true;
    outcome
}

fn fairness_deadline() -> Outcome {
    let mut outcome = Outcome::default();
    for deadline in [false, true] {
        let state = Arc::new(State::default());
        let (mut owner, stop, commands, mut events) = channels(16, 4).unwrap();
        commands
            .try_submit(Command::Scan, if deadline { 1 } else { 1000 })
            .unwrap();
        for _ in 0..9 {
            events
                .try_emit(event(EventKind::LinkChanged(LinkState::Associated)))
                .unwrap();
        }
        if !deadline {
            events.try_emit(result_event()).unwrap();
        }
        events
            .try_emit(completion(if deadline { 0 } else { 1 }))
            .unwrap();
        let worker = spawn("selected-owner", 180, {
            let state = state.clone();
            move |_| {
                let mut controller = Controller::new(FixtureWifi(state));
                let (mut links, mut before_ordinary, mut expired, mut handled) = (0, 0, false, 0);
                let mut clock = 0;
                owner
                    .run(
                        &mut controller,
                        &mut |report| {
                            match report {
                                OwnerReport::CommandAccepted(_) => handled += 1,
                                OwnerReport::State {
                                    link: LinkState::Associated,
                                    ..
                                } => {
                                    links += 1;
                                    handled += 1;
                                }
                                OwnerReport::Update(Update::ScanResult(value)) => {
                                    assert_eq!(value.rssi_dbm, Some(-42));
                                    before_ordinary = links;
                                    handled += 1;
                                }
                                OwnerReport::Update(Update::Finished(value))
                                    if value.result == Err(Error::Timeout) =>
                                {
                                    assert!(deadline);
                                    assert_eq!(links, 0);
                                    expired = true;
                                }
                                OwnerReport::Update(Update::Finished(value)) => {
                                    assert!(!deadline);
                                    assert!(value.result.is_ok());
                                    handled += 1;
                                    stop.request_stop().unwrap();
                                }
                                OwnerReport::Update(Update::Released(_)) => {
                                    assert!(deadline && expired);
                                    handled += 1;
                                    stop.request_stop().unwrap();
                                }
                                _ => {}
                            }
                            Ok(())
                        },
                        || {
                            if deadline {
                                clock += 1;
                            }
                            clock
                        },
                    )
                    .unwrap();
                assert_eq!(links, if deadline { 7 } else { 9 });
                if !deadline {
                    assert!(before_ordinary <= 7);
                }
                controller.finish().unwrap();
                (handled, before_ordinary, expired)
            }
        });
        let (handled, before_ordinary, expired) = join(worker, &mut outcome);
        assert_eq!(handled, if deadline { 9 } else { 12 });
        assert_eq!(state.finishes.load(SeqCst), 1);
        assert_eq!(
            events.try_emit(result_event()).unwrap_err().error,
            DeliveryError::Closed
        );
        let admitted = if deadline { 11 } else { 12 };
        outcome.attempted += admitted;
        outcome.admitted += admitted;
        outcome.handled += handled;
        outcome.discarded += admitted - handled;
        outcome.fairness_links_before_ordinary =
            outcome.fairness_links_before_ordinary.max(before_ordinary);
        outcome.deadline_observations += usize::from(expired);
        outcome.stop_requests += 1;
    }
    outcome.endpoints_closed = true;
    outcome
}

fn failure_recovery() -> Outcome {
    let mut outcome = Outcome::default();
    let state = Arc::new(State::default());
    let (mut owner, _stop, commands, mut events) = channels(1, 1).unwrap();
    events
        .try_emit(event(EventKind::LinkChanged(LinkState::Associated)))
        .unwrap();
    let fault = event(EventKind::Status(ProviderStatus::Failed(Error::Io)));
    let rejected = events.try_emit(fault).unwrap_err();
    assert_eq!(rejected.value, fault);
    assert_eq!(rejected.error, DeliveryError::Full);
    let mut controller = Controller::new(FixtureWifi(state.clone()));
    state.fault.store(true, SeqCst);
    state.stop_failures.store(1, SeqCst);
    let worker = spawn("selected-owner", 180, {
        move |_| {
            // Begin from Ready so the retained failure is observed by reconcile.
            let result = owner.run(
                &mut controller,
                &mut |report| {
                    Err(Rejected {
                        value: report,
                        error: DeliveryError::Full,
                    })
                },
                || 0,
            );
            assert!(matches!(result, Err(OwnerError::Observer(_))));
            assert_eq!(controller.fault(), Some(Error::Io));
            let result = owner.run(&mut controller, &mut |_| Ok(()), || 0);
            assert!(matches!(result, Err(OwnerError::Stop(Error::Io))));
            owner.run(&mut controller, &mut |_| Ok(()), || 0).unwrap();
            assert_eq!(controller.status(), ProviderStatus::Stopped);
            assert_eq!(controller.fault(), Some(Error::Io));
            controller.finish().unwrap();
            controller.finish().unwrap(); // Actual Controller's idempotent finish.
        }
    });
    join(worker, &mut outcome);
    assert_eq!(state.finishes.load(SeqCst), 1);
    assert_eq!(
        events.try_emit(result_event()).unwrap_err().error,
        DeliveryError::Closed
    );
    assert_eq!(
        commands.try_submit(Command::Scan, 1).unwrap_err().error,
        DeliveryError::Closed
    );
    outcome.attempted = 2;
    outcome.admitted = 1;
    outcome.rejected = 1;
    outcome.discarded = 1;
    outcome.failure_observations = 3; // Retained source fault, observer refusal, stop refusal.
    outcome.stop_requests = 2;
    outcome.endpoints_closed = true;
    outcome
}
