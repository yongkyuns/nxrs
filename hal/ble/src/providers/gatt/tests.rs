use super::*;
use backend::{Command as Action, Event as BackendEvent, Kind, Request as Operation};
use std::{
    collections::VecDeque,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, Mutex, OnceLock,
    },
};
#[derive(Default)]
struct State {
    open_error: Option<Error>,
    wake: Option<Wake>,
    open_calls: usize,
    close_calls: usize,
    submit_calls: usize,
    admitted: Option<Operation>,
    events: VecDeque<BackendEvent>,
    recoveries: std::vec::Vec<(SessionId, OperationId)>,
}
static STATE: OnceLock<Mutex<State>> = OnceLock::new();
static SERIAL: Mutex<()> = Mutex::new(());
fn state() -> &'static Mutex<State> {
    STATE.get_or_init(|| Mutex::new(State::default()))
}
fn reset(open_error: Option<Error>) {
    let old = {
        let mut s = state().lock().unwrap();
        core::mem::replace(
            &mut *s,
            State {
                open_error,
                ..State::default()
            },
        )
    };
    drop(old);
}
pub(crate) struct FakeBackend;
impl Backend for FakeBackend {
    fn open(_: SessionId, _: usize, wake: Wake) -> Result<(), Error> {
        let mut s = state().lock().unwrap();
        s.open_calls += 1;
        if let Some(error) = s.open_error {
            return Err(error);
        }
        s.wake = Some(wake);
        Ok(())
    }
    fn submit(_: SessionId, request: Operation) -> Result<(), Error> {
        let mut s = state().lock().unwrap();
        s.submit_calls += 1;
        s.admitted = Some(request);
        Ok(())
    }
    fn poll(_: SessionId) -> Option<BackendEvent> {
        state().lock().unwrap().events.pop_front()
    }
    fn recover(session: SessionId, operation: OperationId) {
        state()
            .lock()
            .unwrap()
            .recoveries
            .push((session, operation));
    }
    fn close(_: SessionId) {
        state().lock().unwrap().close_calls += 1;
    }
}
fn open<const N: usize>(
    wake: impl Fn() + Send + Sync + 'static,
) -> Result<Native<N, FakeBackend>, Error> {
    open_with::<N, FakeBackend, NativePeer, _>(wake)
}
fn wake() {
    let w = state().lock().unwrap().wake.clone();
    w.unwrap().notify();
}
fn release() {
    let w = state().lock().unwrap().wake.take();
    drop(w);
}
struct Counted(Arc<AtomicUsize>);
impl Drop for Counted {
    fn drop(&mut self) {
        self.0.fetch_add(1, Ordering::SeqCst);
    }
}
fn counted(
    drops: &Arc<AtomicUsize>,
    wakes: &Arc<AtomicUsize>,
) -> impl Fn() + Send + Sync + 'static {
    let capture = Counted(drops.clone());
    let wakes = wakes.clone();
    move || {
        let _keep = &capture;
        wakes.fetch_add(1, Ordering::SeqCst);
    }
}
fn request(session: SessionId) -> Request<20> {
    Request {
        id: RequestId {
            session,
            operation: OperationId::new(9).unwrap(),
        },
        link: None,
        command: Command::Connect {
            peer: crate::gatt::Peer {
                session: SessionId::new(7).unwrap(),
                id: crate::PeerId(11),
            },
        },
    }
}
#[test]
fn failed_open_releases_capture_once_and_oversized_capacity_never_acquires() {
    let _serial = SERIAL.lock().unwrap();
    for error in [Error::InvalidValue, Error::Busy] {
        reset(Some(error));
        let drops = Arc::new(AtomicUsize::new(0));
        let wakes = Arc::new(AtomicUsize::new(0));
        assert_eq!(open::<20>(counted(&drops, &wakes)).err(), Some(error));
        assert_eq!(drops.load(Ordering::SeqCst), 1);
        assert_eq!(wakes.load(Ordering::SeqCst), 0);
        assert_eq!(state().lock().unwrap().close_calls, 0);
    }
    reset(None);
    assert_eq!(open::<65>(|| {}).err(), Some(Error::Unsupported));
    assert_eq!(state().lock().unwrap().open_calls, 0);
}
#[test]
fn close_retains_wake_until_the_backend_retirement_barrier() {
    let _serial = SERIAL.lock().unwrap();
    reset(None);
    let drops = Arc::new(AtomicUsize::new(0));
    let wakes = Arc::new(AtomicUsize::new(0));
    let driver = open::<20>(counted(&drops, &wakes)).unwrap();
    wake();
    drop(driver);
    assert_eq!(state().lock().unwrap().close_calls, 1);
    assert_eq!(drops.load(Ordering::SeqCst), 0);
    wake();
    assert_eq!(wakes.load(Ordering::SeqCst), 2);
    release();
    assert_eq!(drops.load(Ordering::SeqCst), 1);
}
#[test]
fn submit_recover_and_poll_use_typed_commands_and_events() {
    let _serial = SERIAL.lock().unwrap();
    reset(None);
    let mut driver = open::<20>(|| {}).unwrap();
    let r = request(driver.session());
    driver.submit(r).unwrap();
    driver.recover(r.id);
    {
        let s = state().lock().unwrap();
        let accepted = s.admitted.unwrap();
        assert_eq!(accepted.operation, 9);
        assert_eq!(accepted.kind(), Kind::Connect);
        assert!(
            matches!(accepted.command,Action::Connect {peer,identity:None} if peer.id.0==11&&peer.session.get()==7)
        );
        assert_eq!(
            s.recoveries.as_slice(),
            &[(driver.session(), r.id.operation)]
        );
    }
    state()
        .lock()
        .unwrap()
        .events
        .push_back(BackendEvent::Completed {
            operation: 9,
            result: Ok(Reply::Connected),
        });
    assert!(
        matches!(driver.poll().unwrap().kind,EventKind::Completed {request,result:Ok(Reply::Connected)} if request==r.id)
    );
    drop(driver);
    release();
}
#[test]
fn wrong_session_cannot_admit_or_recover_work() {
    let _serial = SERIAL.lock().unwrap();
    reset(None);
    let mut driver = open::<20>(|| {}).unwrap();
    let other = SessionId::new(driver.session().get() + 1).unwrap();
    let r = request(other);
    assert_eq!(
        driver.submit(r),
        Err(Rejected {
            value: r,
            error: Error::InvalidValue
        })
    );
    driver.recover(r.id);
    let s = state().lock().unwrap();
    assert_eq!(s.submit_calls, 0);
    assert!(s.recoveries.is_empty());
    drop(s);
    drop(driver);
    release();
}
#[test]
fn resolved_identity_is_copied_before_admission_and_missing_peers_do_not_admit() {
    struct Identity;
    impl PeerResolver for Identity {
        fn resolve(peer: crate::gatt::Peer) -> Result<Option<[u8; 16]>, Error> {
            Ok(Some([peer.id.0 as u8; 16]))
        }
    }
    struct Missing;
    impl PeerResolver for Missing {
        fn resolve(_: crate::gatt::Peer) -> Result<Option<[u8; 16]>, Error> {
            Err(Error::Unavailable)
        }
    }
    let _serial = SERIAL.lock().unwrap();
    reset(None);
    assert_eq!(
        core::mem::size_of::<Native<20, FakeBackend, Identity>>(),
        core::mem::size_of::<Native<20, FakeBackend>>()
    );
    let mut driver = open_with::<20, FakeBackend, Identity, _>(|| {}).unwrap();
    driver.submit(request(driver.session())).unwrap();
    assert!(matches!(
        state().lock().unwrap().admitted.unwrap().command,
        Action::Connect {
            identity: Some([11, ..]),
            ..
        }
    ));
    drop(driver);
    release();
    reset(None);
    let mut driver = open_with::<20, FakeBackend, Missing, _>(|| {}).unwrap();
    let r = request(driver.session());
    assert_eq!(
        driver.submit(r),
        Err(Rejected {
            value: r,
            error: Error::Unavailable
        })
    );
    assert_eq!(state().lock().unwrap().submit_calls, 0);
    drop(driver);
    release();
}
#[test]
fn oversized_values_and_invalid_backend_events_cannot_become_completions_or_barriers() {
    let _serial = SERIAL.lock().unwrap();
    reset(None);
    let mut driver = open::<20>(|| {}).unwrap();
    let cases = [
        BackendEvent::Completed {
            operation: 3,
            result: Ok(Reply::Read(Value::new(&[1; 21]).unwrap())),
        },
        BackendEvent::Fault,
        BackendEvent::Quiescent { recovery: 0 },
        BackendEvent::Completed {
            operation: 0,
            result: Ok(Reply::Written),
        },
    ];
    for event in cases {
        state().lock().unwrap().events.push_back(event);
        assert!(matches!(
            driver.poll().unwrap().kind,
            EventKind::Completed {
                result: Err(Failure::Protocol),
                ..
            }
        ));
    }
    drop(driver);
    release();
}
#[test]
fn att_remote_errors_and_disconnect_reasons_keep_their_meanings() {
    let _serial = SERIAL.lock().unwrap();
    reset(None);
    let mut driver = open::<20>(|| {}).unwrap();
    for failure in [
        Failure::Remote(Error::PermissionDenied),
        Failure::Att(8),
        Failure::Disconnected,
    ] {
        state()
            .lock()
            .unwrap()
            .events
            .push_back(BackendEvent::Completed {
                operation: 3,
                result: Err(failure),
            });
        assert!(
            matches!(driver.poll().unwrap().kind,EventKind::Completed {result:Err(actual),..} if actual==failure)
        );
    }
    for reason in [Some(8), None] {
        state()
            .lock()
            .unwrap()
            .events
            .push_back(BackendEvent::Disconnected {
                connection: 3,
                reason,
            });
        assert!(
            matches!(driver.poll().unwrap().kind,EventKind::Disconnected {reason:actual,..} if actual==reason)
        );
    }
    state()
        .lock()
        .unwrap()
        .events
        .push_back(BackendEvent::Quiescent { recovery: 3 });
    assert!(
        matches!(driver.poll().unwrap().kind,EventKind::Quiescent {recovery} if recovery.operation.get()==3)
    );
    drop(driver);
    release();
}
