#![cfg(all(
    target_os = "linux",
    any(
        all(hal_enabled, target_os = "linux"),
        all(hal_enabled, target_os = "macos")
    )
))]

use std::sync::mpsc::{self, Receiver, SyncSender, TrySendError};
use std::time::{Duration, Instant};
use wifi::{
    self, Command, Completion, Credentials, DeliveryError, Error, Event, EventSink, LinkState,
    Passphrase, ProviderStatus, Rejected, Ssid, Update, Wifi,
};

const OPERATION_TIMEOUT_MS: u64 = 20_000;
const STARTUP_TIMEOUT: Duration = Duration::from_secs(20);

struct Sink(SyncSender<Event>);

impl EventSink<Event> for Sink {
    fn try_emit(&mut self, event: Event) -> Result<(), Rejected<Event, DeliveryError>> {
        self.0.try_send(event).map_err(|error| match error {
            TrySendError::Full(value) => Rejected {
                value,
                error: DeliveryError::Full,
            },
            TrySendError::Disconnected(value) => Rejected {
                value,
                error: DeliveryError::Closed,
            },
        })
    }
}

fn elapsed_ms(start: Instant) -> u64 {
    u64::try_from(start.elapsed().as_millis()).unwrap_or(u64::MAX)
}

fn receive_update<D: Wifi>(
    controller: &mut wifi::Controller<D>,
    events: &Receiver<Event>,
    start: Instant,
    deadline: Instant,
) -> Update {
    let remaining = deadline.saturating_duration_since(Instant::now());
    assert!(!remaining.is_zero(), "event deadline expired");
    let event = events
        .recv_timeout(remaining)
        .expect("native Wi-Fi event did not arrive before its deadline");
    controller
        .on_event(event, elapsed_ms(start))
        .expect("controller clock must remain monotonic")
}

fn wait_ready<D: Wifi>(
    controller: &mut wifi::Controller<D>,
    events: &Receiver<Event>,
    start: Instant,
) {
    let deadline = Instant::now() + STARTUP_TIMEOUT;
    while controller.status() != ProviderStatus::Ready {
        receive_update(controller, events, start, deadline);
        assert!(!matches!(
            controller.status(),
            ProviderStatus::Failed(_) | ProviderStatus::Stopped
        ));
    }
}

fn wait_completion<D: Wifi>(
    controller: &mut wifi::Controller<D>,
    events: &Receiver<Event>,
    start: Instant,
    request: wifi::RequestId,
    expected: Completion,
) {
    let deadline_ms = controller
        .next_deadline_ms()
        .expect("admitted operation retains its controller deadline");
    let deadline = start + Duration::from_millis(deadline_ms);
    loop {
        match receive_update(controller, events, start, deadline) {
            Update::Finished(outcome) if outcome.id == request => {
                assert_eq!(outcome.result, Ok(expected));
                return;
            }
            Update::Finished(outcome) => panic!("unexpected operation outcome: {outcome:?}"),
            _ => {}
        }
    }
}

fn connect_command() -> Command {
    Command::Connect {
        ssid: Ssid::new(&[0xff, 0, 0x7f]).unwrap(),
        credentials: Credentials::Wpa2Personal(Passphrase::new("fixture-only-password").unwrap()),
    }
}

#[test]
#[ignore = "requires the private NetworkManager test service"]
fn station_owner() {
    // The parent C runner sets this only for the child process that owns its
    // private GTestDBus. Check before opening anything that can reach the bus.
    assert_eq!(
        std::env::var("NXRS_PRIVATE_RADIO_TEST").as_deref(),
        Ok("1"),
        "run only through the private Linux NetworkManager test runner"
    );

    let (sender, events) = mpsc::sync_channel(32);
    let start = Instant::now();
    let mut controller = wifi::open(Sink(sender)).expect("private fake NetworkManager opens");
    wait_ready(&mut controller, &events, start);

    let request = controller
        .submit(connect_command(), elapsed_ms(start), OPERATION_TIMEOUT_MS)
        .expect("WPA2 activation request is admitted");
    wait_completion(
        &mut controller,
        &events,
        start,
        request,
        Completion::Connected,
    );
    assert_eq!(controller.link(), LinkState::Addressed);

    let disconnect = controller
        .submit(Command::Disconnect, elapsed_ms(start), OPERATION_TIMEOUT_MS)
        .expect("disconnect request is admitted");
    wait_completion(
        &mut controller,
        &events,
        start,
        disconnect,
        Completion::Disconnected,
    );
    assert_eq!(controller.link(), LinkState::Disconnected);

    let second_connect = controller
        .submit(connect_command(), elapsed_ms(start), OPERATION_TIMEOUT_MS)
        .expect("second WPA2 activation request is admitted");
    wait_completion(
        &mut controller,
        &events,
        start,
        second_connect,
        Completion::Connected,
    );
    assert_eq!(controller.link(), LinkState::Addressed);

    // Shutdown must release the activation still owned by this session.
    controller.request_stop().expect("stop request is accepted");
    let stop_deadline = Instant::now() + STARTUP_TIMEOUT;
    let mut cleanup_failed = false;
    while controller.status() != ProviderStatus::Stopped {
        receive_update(&mut controller, &events, start, stop_deadline);
        if controller.status() == ProviderStatus::Failed(Error::Io) {
            // The fake daemon holds the first shutdown deactivation active.
            // Neither the native context nor the interface lease may be lost.
            assert!(!cleanup_failed, "cleanup must recover on explicit retry");
            assert_eq!(controller.finish(), Err(Error::Busy));
            let (other_sender, _other_events) = mpsc::sync_channel(1);
            assert!(matches!(wifi::open(Sink(other_sender)), Err(Error::Busy)));
            cleanup_failed = true;
            controller
                .request_stop()
                .expect("cleanup retry is admitted");
        }
    }
    assert!(
        cleanup_failed,
        "the injected cleanup failure must be observed"
    );
    controller
        .finish()
        .expect("worker joins and native activation cleanup succeeds");
    assert_eq!(controller.finish(), Ok(()));
    assert_eq!(controller.fault(), Some(Error::Io));
}
