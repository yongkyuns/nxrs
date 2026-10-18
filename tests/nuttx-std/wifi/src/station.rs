//! Hardware qualification only: runtime credentials, DHCP, exact UDP echoes,
//! then explicit disconnect and worker join. Not connectivity-service policy.
use crossbeam_channel::{bounded, Receiver, Sender, TryRecvError, TrySendError};
#[cfg(target_os = "nuttx")]
use std::net::{SocketAddrV4, UdpSocket};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::Arc;
#[cfg(target_os = "nuttx")]
use std::time::{Duration, Instant};
#[cfg(target_os = "nuttx")]
use wifi::{Command, Completion, Credentials, Passphrase, Ssid, Update};
use wifi::{
    DeliveryError, Error, Event, EventKind, EventSink, LinkState, ProviderStatus, Rejected,
};

#[cfg(target_os = "nuttx")]
unsafe extern "C" {
    fn nxrs_wifi_test_credentials(
        ssid: *mut u8,
        password: *mut u8,
        ssid_length: *mut u32,
        password_length: *mut u32,
    ) -> i32;
}

struct ObservedState {
    status: AtomicU32,
    link: AtomicU32,
    address_lost: AtomicBool,
}

/// Test-only tap of provider observations, not a new radio poll or delivery ACK.
#[derive(Clone)]
pub struct Observation(Arc<ObservedState>);

impl Observation {
    fn new() -> Self {
        Self(Arc::new(ObservedState {
            status: AtomicU32::new(0),
            link: AtomicU32::new(0),
            address_lost: AtomicBool::new(false),
        }))
    }

    fn record(&self, kind: EventKind) {
        match kind {
            EventKind::Status(status) => {
                let value = match status {
                    ProviderStatus::Starting => 0,
                    ProviderStatus::Ready => 1,
                    ProviderStatus::Stopping => 2,
                    ProviderStatus::Stopped => 3,
                    ProviderStatus::Failed(_) => 4,
                };
                self.0.status.store(value, Ordering::Release);
            }
            EventKind::LinkChanged(link) => {
                let value = match link {
                    LinkState::Unknown => 0,
                    LinkState::Disconnected => 1,
                    LinkState::Associated => 2,
                    LinkState::Addressed => 3,
                };
                // There is one writer: the sink owned by the provider worker.
                // Remember a transient loss even if Addressed is restored later.
                if self.0.link.load(Ordering::Relaxed) == 3 && value != 3 {
                    self.0.address_lost.store(true, Ordering::Release);
                }
                self.0.link.store(value, Ordering::Release);
            }
            _ => {}
        }
    }

    pub fn report(&self, stage: &str) {
        let (status, link, address_lost) = self.snapshot();
        let status = ["Starting", "Ready", "Stopping", "Stopped", "Failed"][status as usize];
        let link = ["Unknown", "Disconnected", "Associated", "Addressed"][link as usize];
        println!(
            "NXRS_WIFI_OBSERVATION stage={stage} status={status} link={link} address_lost={}",
            u32::from(address_lost)
        );
    }

    fn snapshot(&self) -> (u32, u32, bool) {
        // Independent atomic fields; this is not an atomic hardware snapshot.
        let link = self.0.link.load(Ordering::Acquire);
        (
            self.0.status.load(Ordering::Acquire),
            link,
            self.0.address_lost.load(Ordering::Acquire),
        )
    }
}

struct Sink {
    events: Sender<Event>,
    observation: Observation,
    owner: std::thread::Thread,
}
impl EventSink<Event> for Sink {
    fn try_emit(&mut self, event: Event) -> Result<(), Rejected<Event, DeliveryError>> {
        // Record attempts even if delivery is backpressured. Keep its result intact.
        self.observation.record(event.kind);
        let result = self.events.try_send(event).map_err(|error| match error {
            TrySendError::Full(value) => Rejected {
                value,
                error: DeliveryError::Full,
            },
            TrySendError::Disconnected(value) => Rejected {
                value,
                error: DeliveryError::Closed,
            },
        });
        // The same owner can wait on GATT and Wi-Fi without timed queue polls.
        // Wake even on backpressure, so it can drain and permit redelivery.
        self.owner.unpark();
        result
    }
}

fn drain_connection<D: wifi::Wifi>(
    controller: &mut wifi::Controller<D>,
    events: &Receiver<Event>,
    now: u64,
) -> Result<(), Error> {
    loop {
        match events.try_recv() {
            Ok(event) => {
                if let wifi::Update::Finished(outcome) = controller
                    .on_event(event, now)
                    .map_err(|_| Error::Protocol)?
                {
                    outcome.result?;
                }
                if let Some(error) = controller.fault() {
                    return Err(error);
                }
                // Do not hide a transient loss behind a later Addressed event.
                if controller.link() != LinkState::Addressed {
                    return Err(Error::Unavailable);
                }
            }
            Err(TryRecvError::Empty) => break,
            Err(TryRecvError::Disconnected) => return Err(Error::Unavailable),
        }
    }
    controller.reconcile().map_err(|_| Error::Protocol)?;
    if let Some(error) = controller.fault() {
        return Err(error);
    }
    if controller.status() != ProviderStatus::Ready || controller.link() != LinkState::Addressed {
        return Err(Error::Unavailable);
    }
    Ok(())
}

fn disconnect_after<T>(
    traffic: Result<T, Error>,
    disconnect: impl FnOnce() -> Result<(), Error>,
) -> Result<T, Error> {
    let cleanup = disconnect();
    if let Err(error) = cleanup {
        eprintln!("NXRS_WIFI_STAGE_FAIL stage=disconnect error={error:?}");
    }
    traffic.and_then(|value| cleanup.map(|()| value))
}

#[cfg(target_os = "nuttx")]
fn credentials() -> Result<Command, Error> {
    let mut ssid = [0; 32];
    let mut password = [0; 63];
    let (mut ssid_length, mut password_length) = (0, 0);
    // SAFETY: writable arrays/scalars match the fixed-width C ABI. The helper
    // disables console echo before reading and retains no pointers.
    let status = unsafe {
        nxrs_wifi_test_credentials(
            ssid.as_mut_ptr(),
            password.as_mut_ptr(),
            &mut ssid_length,
            &mut password_length,
        )
    };
    let result = (|| {
        if status != 0 {
            return Err(Error::Io);
        }
        let ssid = Ssid::new(ssid.get(..ssid_length as usize).ok_or(Error::Protocol)?)?;
        let bytes = password
            .get(..password_length as usize)
            .ok_or(Error::Protocol)?;
        let credentials = if bytes.is_empty() {
            Credentials::Open
        } else {
            Credentials::Wpa2Personal(Passphrase::new(
                std::str::from_utf8(bytes).map_err(|_| Error::InvalidConfig)?,
            )?)
        };
        Ok(Command::Connect { ssid, credentials })
    })();
    // Best-effort clearing of the fixture input copy; the native driver still
    // needs credentials in RAM. This is not a claim of whole-stack zeroization.
    password.fill(0);
    result
}

#[cfg(target_os = "nuttx")]
pub fn run(peer: SocketAddrV4) -> Result<(), Error> {
    if peer.ip().is_unspecified() || peer.ip().is_multicast() || peer.port() == 0 {
        return Err(Error::InvalidConfig);
    }
    with_connection(|| echo(peer))?;
    println!("NXRS_WIFI_STATION_PASS echoes=20 disconnect=confirmed shutdown=joined");
    Ok(())
}

/// Run another radio qualification while the existing station is addressed.
/// Both success and traffic failure explicitly disconnect and join the provider.
#[cfg(target_os = "nuttx")]
pub fn with_connection<T>(traffic: impl FnOnce() -> Result<T, Error>) -> Result<T, Error> {
    with_connection_observed(|_| traffic())
}

/// Same station lifetime, with a read-only provider event tap for radio tests.
#[cfg(target_os = "nuttx")]
pub fn with_connection_observed<T>(
    traffic: impl FnOnce(Observation) -> Result<T, Error>,
) -> Result<T, Error> {
    with_connection_reactor(|observation, _| traffic(observation))
}

/// A composed fixture drains Wi-Fi from its existing event loop. The sink wakes
/// this thread; the supplied poll function never waits on an additional queue.
#[cfg(target_os = "nuttx")]
pub fn with_connection_reactor<T>(
    traffic: impl FnOnce(Observation, &mut dyn FnMut() -> Result<(), Error>) -> Result<T, Error>,
) -> Result<T, Error> {
    let command = credentials()?;
    let (tx, rx) = bounded(8);
    let observation = Observation::new();
    let mut controller = wifi::open(Sink {
        events: tx,
        observation: observation.clone(),
        owner: std::thread::current(),
    })?;
    let start = Instant::now();
    let clock = || u64::try_from(start.elapsed().as_millis()).unwrap();
    let wait =
        |controller: &mut wifi::Controller<_>, expected: Option<Completion>| -> Result<(), Error> {
            let deadline = Instant::now() + Duration::from_secs(30);
            loop {
                controller.reconcile().map_err(|_| Error::Protocol)?;
                if let ProviderStatus::Failed(error) = controller.status() {
                    return Err(error);
                }
                if expected.is_none() && controller.status() == ProviderStatus::Ready {
                    return Ok(());
                }
                let remaining = deadline.saturating_duration_since(Instant::now());
                if remaining.is_zero() {
                    return Err(Error::Timeout);
                }
                let event = rx.recv_timeout(remaining).map_err(|_| Error::Timeout)?;
                let update = controller
                    .on_event(event, clock())
                    .map_err(|_| Error::Protocol)?;
                let timeout = controller.advance(clock()).map_err(|_| Error::Protocol)?;
                if let Update::Finished(outcome) = timeout {
                    return outcome.result.map(|_| ());
                }
                if let Update::Finished(outcome) = update {
                    return if Some(outcome.result?) == expected {
                        Ok(())
                    } else {
                        Err(Error::Protocol)
                    };
                }
            }
        };
    let mut phase = "ready";
    let result = (|| {
        wait(&mut controller, None)?;
        phase = "submit";
        controller
            .submit(command, clock(), 25_000)
            .map_err(|rejected| match rejected.error {
                wifi::ControllerError::Device(error) => error,
                _ => Error::Protocol,
            })?;
        phase = "connect";
        wait(&mut controller, Some(Completion::Connected))?;
        if controller.link() != LinkState::Addressed {
            return Err(Error::Protocol);
        }
        println!("NXRS_WIFI_ASSOCIATED_DHCP");
        let traffic = {
            let mut poll = || drain_connection(&mut controller, &rx, clock());
            traffic(observation.clone(), &mut poll)
        };
        let traffic_failed = traffic.is_err();
        // Even failed traffic must explicitly disconnect, not merely stop callbacks.
        phase = "disconnect";
        let result = disconnect_after(traffic, || {
            controller
                .submit(Command::Disconnect, clock(), 5_000)
                .map_err(|_| Error::Protocol)?;
            wait(&mut controller, Some(Completion::Disconnected))?;
            if controller.link() != LinkState::Disconnected {
                return Err(Error::Protocol);
            }
            Ok(())
        });
        if traffic_failed || result.is_ok() {
            phase = "traffic";
        }
        result
    })();
    if let Err(error) = &result {
        eprintln!("NXRS_WIFI_STAGE_FAIL stage={phase} error={error:?}");
        // Traffic errors are returned after the explicit disconnect. Do not
        // present that expected link loss as the cause of the traffic failure.
        observation.report(if phase == "traffic" { "cleanup" } else { phase });
    }
    controller.request_stop().map_err(|_| Error::Io)?;
    // Native connection/DHCP waits are bounded but not preemptible. Drain before
    // join, even after an operation/traffic failure. No false PASS on cleanup.
    let deadline = Instant::now() + Duration::from_secs(30);
    while controller.status() != ProviderStatus::Stopped {
        controller.reconcile().map_err(|_| Error::Protocol)?;
        if controller.status() == ProviderStatus::Stopped {
            break;
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(Error::Timeout);
        }
        let event = rx.recv_timeout(remaining).map_err(|_| Error::Io)?;
        controller
            .on_event(event, clock())
            .map_err(|_| Error::Protocol)?;
    }
    controller.finish().map_err(|_| Error::Io)?;
    result
}

#[cfg(test)]
mod observation_tests {
    use super::*;
    use std::{cell::Cell, rc::Rc};

    struct FakeWifi(Rc<Cell<ProviderStatus>>);
    impl wifi::Wifi for FakeWifi {
        fn session(&self) -> wifi::SessionId {
            wifi::SessionId::new(5).unwrap()
        }
        fn capabilities(&self) -> wifi::Capabilities {
            wifi::Capabilities {
                scan: true,
                open_network: true,
                wpa2_personal: true,
                credential_storage: wifi::CredentialStorage::Volatile,
            }
        }
        fn status(&self) -> ProviderStatus {
            self.0.get()
        }
        fn fault(&self) -> Option<Error> {
            if let ProviderStatus::Failed(error) = self.status() {
                Some(error)
            } else {
                None
            }
        }
        fn submit(&mut self, request: wifi::Request) -> Result<(), Rejected<wifi::Command, Error>> {
            Err(Rejected {
                value: request.command,
                error: Error::Unsupported,
            })
        }
        fn request_stop(&mut self) -> Result<(), Error> {
            Ok(())
        }
        fn finish(&mut self) -> Result<(), Error> {
            Ok(())
        }
    }

    struct Connected {
        controller: wifi::Controller<FakeWifi>,
        sender: Sender<Event>,
        receiver: Receiver<Event>,
        status: Rc<Cell<ProviderStatus>>,
    }
    impl Connected {
        fn new() -> Self {
            let status = Rc::new(Cell::new(ProviderStatus::Ready));
            let mut controller = wifi::Controller::new(FakeWifi(status.clone()));
            controller
                .on_event(
                    Event {
                        session: controller.session(),
                        kind: EventKind::LinkChanged(LinkState::Addressed),
                    },
                    0,
                )
                .unwrap();
            let (sender, receiver) = bounded(8);
            Self {
                controller,
                sender,
                receiver,
                status,
            }
        }
        fn emit(&self, kind: EventKind) {
            if let EventKind::Status(status) = kind {
                self.status.set(status);
            }
            self.sender
                .send(Event {
                    session: self.controller.session(),
                    kind,
                })
                .unwrap();
        }
        fn poll(&mut self) -> Result<(), Error> {
            drain_connection(&mut self.controller, &self.receiver, 1)
        }
    }

    #[test]
    fn reactor_returns_immediately_for_an_empty_live_queue() {
        let mut connection = Connected::new();
        assert_eq!(connection.poll(), Ok(()));
        assert_eq!(connection.poll(), Ok(()));
    }

    #[test]
    fn reactor_does_not_hide_transient_link_loss_or_provider_fault() {
        let mut connection = Connected::new();
        connection.emit(EventKind::LinkChanged(LinkState::Associated));
        connection.emit(EventKind::LinkChanged(LinkState::Addressed));
        assert_eq!(connection.poll(), Err(Error::Unavailable));
        assert_eq!(connection.receiver.len(), 1);

        let mut connection = Connected::new();
        connection.emit(EventKind::Status(ProviderStatus::Failed(Error::Io)));
        assert_eq!(connection.poll(), Err(Error::Io));
    }

    #[test]
    fn reactor_reconciles_terminal_snapshots_and_rejects_closed_delivery() {
        let mut connection = Connected::new();
        connection.status.set(ProviderStatus::Stopped);
        assert_eq!(connection.poll(), Err(Error::Unavailable));

        let Connected {
            mut controller,
            sender,
            receiver,
            ..
        } = Connected::new();
        drop(sender);
        assert_eq!(
            drain_connection(&mut controller, &receiver, 1),
            Err(Error::Unavailable)
        );
    }

    #[test]
    fn reactor_ignores_another_sessions_events() {
        let mut connection = Connected::new();
        connection
            .sender
            .send(Event {
                session: wifi::SessionId::new(6).unwrap(),
                kind: EventKind::LinkChanged(LinkState::Disconnected),
            })
            .unwrap();
        assert_eq!(connection.poll(), Ok(()));
        assert!(connection.receiver.is_empty());
    }

    #[test]
    fn failed_traffic_still_disconnects_and_preserves_the_first_error() {
        for (traffic, cleanup, expected) in [
            (Err(Error::Protocol), Err(Error::Io), Err(Error::Protocol)),
            (Err(Error::Protocol), Ok(()), Err(Error::Protocol)),
            (Ok(7), Err(Error::Io), Err(Error::Io)),
            (Ok(7), Ok(()), Ok(7)),
        ] {
            let called = Cell::new(false);
            assert_eq!(
                disconnect_after(traffic, || {
                    called.set(true);
                    cleanup
                }),
                expected
            );
            assert!(called.get());
        }
    }

    #[test]
    fn status_and_link_are_recorded_without_commands_or_payloads() {
        let observation = Observation::new();
        assert_eq!(observation.snapshot(), (0, 0, false));
        observation.record(EventKind::Status(ProviderStatus::Ready));
        observation.record(EventKind::LinkChanged(LinkState::Addressed));
        assert_eq!(observation.clone().snapshot(), (1, 3, false));
        observation.record(EventKind::Status(ProviderStatus::Failed(Error::Io)));
        assert_eq!(observation.snapshot(), (4, 3, false));
    }

    #[test]
    fn transient_address_loss_stays_visible_after_recovery() {
        let observation = Observation::new();
        observation.record(EventKind::LinkChanged(LinkState::Disconnected));
        observation.record(EventKind::LinkChanged(LinkState::Addressed));
        observation.record(EventKind::LinkChanged(LinkState::Addressed));
        assert_eq!(observation.snapshot(), (0, 3, false));
        observation.record(EventKind::LinkChanged(LinkState::Associated));
        observation.record(EventKind::LinkChanged(LinkState::Addressed));
        assert_eq!(observation.snapshot(), (0, 3, true));
    }

    #[test]
    fn the_tap_preserves_full_and_closed_delivery_results() {
        let (events, received) = bounded(1);
        let observation = Observation::new();
        let mut sink = Sink {
            events,
            observation: observation.clone(),
            owner: std::thread::current(),
        };
        let mut event = Event {
            session: wifi::SessionId::new(1).unwrap(),
            kind: EventKind::LinkChanged(LinkState::Addressed),
        };
        sink.try_emit(event).unwrap();
        event.kind = EventKind::LinkChanged(LinkState::Associated);
        let rejected = sink.try_emit(event).unwrap_err();
        assert_eq!(rejected.error, DeliveryError::Full);
        assert_eq!(rejected.value, event);
        assert_eq!(observation.snapshot(), (0, 2, true));
        drop(received);
        let rejected = sink.try_emit(event).unwrap_err();
        assert_eq!(rejected.error, DeliveryError::Closed);
        assert_eq!(rejected.value, event);
    }
}

#[cfg(target_os = "nuttx")]
fn echo(peer: SocketAddrV4) -> Result<(), Error> {
    let socket = UdpSocket::bind("0.0.0.0:0").map_err(|_| Error::Io)?;
    socket
        .set_read_timeout(Some(Duration::from_secs(2)))
        .map_err(|_| Error::Io)?;
    socket
        .set_write_timeout(Some(Duration::from_secs(2)))
        .map_err(|_| Error::Io)?;
    socket.connect(peer).map_err(|_| Error::Io)?;
    let mut sent = [0; 512];
    let mut received = [0; 513];
    for sequence in 0u32..20 {
        let size = [16, 64, 256, 512][sequence as usize % 4];
        sent[..4].copy_from_slice(&sequence.to_be_bytes());
        for (offset, byte) in sent[4..size].iter_mut().enumerate() {
            *byte = (offset as u8).wrapping_add(sequence as u8);
        }
        if socket.send(&sent[..size]).map_err(|error| {
            eprintln!(
                "NXRS_WIFI_TRAFFIC_FAIL phase=send sequence={sequence} kind={:?}",
                error.kind()
            );
            Error::Io
        })? != size
        {
            return Err(Error::Protocol);
        }
        let length = socket.recv(&mut received).map_err(|error| {
            eprintln!(
                "NXRS_WIFI_TRAFFIC_FAIL phase=receive sequence={sequence} kind={:?}",
                error.kind()
            );
            Error::Timeout
        })?;
        if received[..length] != sent[..size] {
            return Err(Error::Protocol);
        }
    }
    Ok(())
}
