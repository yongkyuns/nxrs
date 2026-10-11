//! Scan qualification through the portable BLE controller. No GATT pass claim.
#![forbid(unsafe_code)]

use ble::{
    Command, Completion, DeliveryError, Error, Event, EventKind, EventSink, ProviderStatus,
    Rejected, Update,
};
use std::sync::mpsc::{self, SyncSender, TrySendError};
use std::time::{Duration, Instant};

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

fn run(inject_failure: bool) -> Result<(u32, bool), Error> {
    let (sender, receiver) = mpsc::sync_channel(8);
    let mut controller = ble::open(Sink(sender))?;
    let epoch = Instant::now();
    let now = || epoch.elapsed().as_millis() as u64;
    let mut scanning_until = None;
    let mut started = false;
    let mut scan_stopped = false;
    let mut observed = 0;
    let mut discovery_limited = false;
    let mut failure = None;
    let mut shutdown_deadline = None;
    loop {
        let time = now();
        // Reconcile the sticky snapshot even if a full queue prevented its
        // terminal wakeup. All native waits remain on the provider worker.
        controller.reconcile().map_err(|_| Error::Io)?;
        let status = controller.status();
        if status == ProviderStatus::Stopped {
            controller.finish()?;
            return if let Some(error) = failure.or(controller.fault()) {
                Err(error)
            } else if scan_stopped && observed > 0 {
                Ok((observed, discovery_limited))
            } else {
                Err(Error::Unavailable)
            };
        }
        if shutdown_deadline.is_some_and(|deadline| time >= deadline) {
            return Err(Error::Timeout); // no PASS or false worker-join claim
        }
        if failure.is_none() && !started && status == ProviderStatus::Ready {
            controller
                .submit(Command::StartScan { service: None }, time, 5_000)
                .map_err(|_| Error::Io)?;
            started = true;
        }
        if scanning_until.is_some_and(|deadline| time >= deadline) && !controller.is_busy() {
            controller
                .submit(Command::StopScan, time, 5_000)
                .map_err(|_| Error::Io)?;
            scanning_until = None;
        }
        let wait_until = scanning_until
            .into_iter()
            .chain(controller.next_deadline_ms())
            .chain(shutdown_deadline)
            .min()
            .unwrap_or(time + 100);
        let delay = Duration::from_millis(wait_until.saturating_sub(time).min(100));
        let update = match receiver.recv_timeout(delay) {
            Ok(event) => controller.on_event(event, now()).map_err(|_| Error::Io)?,
            Err(mpsc::RecvTimeoutError::Timeout) => {
                controller.advance(now()).map_err(|_| Error::Io)?
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => return Err(Error::Unavailable),
        };
        match update {
            Update::Event(EventKind::Discovered { .. }) => observed += 1,
            Update::Event(EventKind::DiscoveryLimited { .. }) => discovery_limited = true,
            Update::Finished(outcome) => match outcome.result {
                Ok(Completion::ScanStarted) => scanning_until = Some(now() + 8_000),
                Ok(Completion::ScanStopped) => {
                    scan_stopped = true;
                    if inject_failure {
                        failure = Some(Error::Io);
                    }
                    controller.request_stop()?;
                    shutdown_deadline = Some(now() + 5_000);
                }
                Err(error) => {
                    failure = Some(error);
                    controller.request_stop()?;
                    shutdown_deadline = Some(now() + 5_000);
                }
            },
            _ => {}
        }
        if controller.fault().is_some() && shutdown_deadline.is_none() {
            failure = controller.fault();
            controller.request_stop()?;
            shutdown_deadline = Some(now() + 5_000);
        }
        // Startup also has a bound; a missing controller cannot hang NSH.
        if !started && now() >= 5_000 && shutdown_deadline.is_none() {
            failure = Some(Error::Timeout);
            controller.request_stop()?;
            shutdown_deadline = Some(now() + 5_000);
        }
    }
}

fn main() {
    let fail = match std::env::args().nth(1).as_deref() {
        None => false,
        Some("fail") => true,
        _ => {
            eprintln!("usage: ble_test [fail]");
            std::process::exit(2);
        }
    };
    match run(fail) {
        Ok((peers, limited)) => {
            println!("NXRS_BLE_SCAN_PASS peers={peers} limited={limited} shutdown=joined")
        }
        Err(error) => {
            eprintln!("NXRS_BLE_SCAN_FAIL error={error:?}");
            std::process::exit(1);
        }
    }
}
