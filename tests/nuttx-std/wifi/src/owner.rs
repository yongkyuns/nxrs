//! Scan fixture's bounded execution owner for [`wifi::Controller`].
//!
//! Build the channels and install the returned event sink before enabling the
//! provider. `run` never creates threads or joins them; after it observes
//! `Stopped`, the caller reclaims provider workers explicitly.
//! This scheduling example is not part of the Wi-Fi HAL API.
#![forbid(unsafe_code)]

use std::time::Duration;

use crossbeam_channel::{bounded, never, Receiver, Sender, TrySendError};
use wifi::{
    Command, Controller, ControllerError, DeliveryError, Error, Event, EventKind, EventSink,
    LinkState, ProviderStatus, Rejected, RequestId, Update,
};

const IMPORTANT_BURST: usize = 8;

#[cfg(test)]
#[path = "tests.rs"]
mod tests;

/// Create the loop and its three independent bounded admission endpoints.
pub fn channels(
    important_capacity: usize,
    ordinary_capacity: usize,
) -> Result<(OwnerLoop, StopHandle, CommandSender, WifiEventSink), ChannelError> {
    if important_capacity == 0 || ordinary_capacity == 0 {
        return Err(ChannelError::ZeroCapacity);
    }

    let (stop_tx, stop_rx) = bounded(1);
    let (important_tx, important_rx) = bounded(important_capacity);
    let (ordinary_tx, ordinary_rx) = bounded(ordinary_capacity);
    Ok((
        OwnerLoop {
            stop_rx,
            important_rx,
            ordinary_rx,
            pending_ready: None,
            important_streak: 0,
        },
        StopHandle { tx: stop_tx },
        CommandSender {
            tx: important_tx.clone(),
        },
        WifiEventSink {
            important: important_tx,
            ordinary: ordinary_tx,
        },
    ))
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChannelError {
    ZeroCapacity,
}

/// Lifecycle-owner-only stop endpoint. It is intentionally not `Clone`.
pub struct StopHandle {
    tx: Sender<()>,
}

impl StopHandle {
    /// Nonblocking and isolated from command and HAL event capacity.
    pub fn request_stop(&self) -> Result<(), StopAdmissionError> {
        match self.tx.try_send(()) {
            Ok(()) => Ok(()),
            Err(TrySendError::Full(())) => Err(StopAdmissionError::AlreadyPending),
            Err(TrySendError::Disconnected(())) => Err(StopAdmissionError::OwnerClosed),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StopAdmissionError {
    AlreadyPending,
    OwnerClosed,
}

/// Cloneable bounded command ingress. Rejection returns the original command.
#[derive(Clone)]
pub struct CommandSender {
    tx: Sender<Important>,
}

impl CommandSender {
    pub fn try_submit(
        &self,
        command: Command,
        timeout_ms: u64,
    ) -> Result<(), Rejected<Command, DeliveryError>> {
        let message = Important::Command {
            command,
            timeout_ms,
        };
        match self.tx.try_send(message) {
            Ok(()) => Ok(()),
            Err(TrySendError::Full(Important::Command { command, .. })) => Err(Rejected {
                value: command,
                error: DeliveryError::Full,
            }),
            Err(TrySendError::Disconnected(Important::Command { command, .. })) => Err(Rejected {
                value: command,
                error: DeliveryError::Closed,
            }),
            Err(_) => unreachable!("command sender only submits commands"),
        }
    }
}

/// Cloneable HAL source endpoint. Admission only; never invokes controller code.
#[derive(Clone)]
pub struct WifiEventSink {
    important: Sender<Important>,
    ordinary: Sender<Event>,
}

impl EventSink<Event> for WifiEventSink {
    fn try_emit(&mut self, event: Event) -> Result<(), Rejected<Event, DeliveryError>> {
        match event.kind {
            EventKind::ScanResult { .. } | EventKind::Completed { .. } => {
                send_event(&self.ordinary, event)
            }
            EventKind::Status(_) | EventKind::LinkChanged(_) => {
                match self.important.try_send(Important::Event(event)) {
                    Ok(()) => Ok(()),
                    Err(TrySendError::Full(Important::Event(event))) => Err(Rejected {
                        value: event,
                        error: DeliveryError::Full,
                    }),
                    Err(TrySendError::Disconnected(Important::Event(event))) => Err(Rejected {
                        value: event,
                        error: DeliveryError::Closed,
                    }),
                    Err(_) => unreachable!("event sink only submits events"),
                }
            }
        }
    }
}

fn send_event(sender: &Sender<Event>, event: Event) -> Result<(), Rejected<Event, DeliveryError>> {
    match sender.try_send(event) {
        Ok(()) => Ok(()),
        Err(TrySendError::Full(event)) => Err(Rejected {
            value: event,
            error: DeliveryError::Full,
        }),
        Err(TrySendError::Disconnected(event)) => Err(Rejected {
            value: event,
            error: DeliveryError::Closed,
        }),
    }
}

enum Important {
    Command { command: Command, timeout_ms: u64 },
    Event(Event),
}

/// Output requested from a bounded, nonblocking observer by the caller.
#[derive(Debug)]
pub enum OwnerReport {
    CommandAccepted(RequestId),
    CommandRejected(Rejected<Command, ControllerError>),
    State {
        status: ProviderStatus,
        link: LinkState,
        fault: Option<Error>,
    },
    Update(Update),
}

pub struct OwnerLoop {
    stop_rx: Receiver<()>,
    important_rx: Receiver<Important>,
    ordinary_rx: Receiver<Event>,
    pending_ready: Option<Ready>,
    important_streak: usize,
}

#[derive(Debug)]
pub enum OwnerError {
    Controller(ControllerError),
    Stop(Error),
    Observer(Rejected<OwnerReport, DeliveryError>),
}

enum Ready {
    Stop(Result<(), crossbeam_channel::RecvError>),
    Important(Result<Important, crossbeam_channel::RecvError>),
    Ordinary(Result<Event, crossbeam_channel::RecvError>),
    Deadline,
}

impl OwnerLoop {
    /// Run until provider quiescence or a recoverable stop/observer/controller
    /// error. `clock_ms` is monotonic milliseconds supplied by the composition
    /// owner; no timer thread or periodic polling is created here.
    pub fn run<D, O, C>(
        &mut self,
        controller: &mut Controller<D>,
        observer: &mut O,
        mut clock_ms: C,
    ) -> Result<(), OwnerError>
    where
        D: wifi::Wifi,
        O: EventSink<OwnerReport>,
        C: FnMut() -> u64,
    {
        let mut stop_requested = false;

        loop {
            let now = clock_ms();
            let update = controller.advance(now).map_err(OwnerError::Controller)?;
            publish_update(observer, controller, update)?;
            let update = controller.reconcile().map_err(OwnerError::Controller)?;
            publish_update(observer, controller, update)?;
            if controller.status() == wifi::ProviderStatus::Stopped {
                self.disable_receivers();
                return Ok(());
            }
            if matches!(controller.status(), wifi::ProviderStatus::Failed(_)) && !stop_requested {
                controller.request_stop().map_err(OwnerError::Stop)?;
                stop_requested = true;
            }

            if self.pending_ready.is_none() {
                let deadline = controller
                    .next_deadline_ms()
                    .map(|at| Duration::from_millis(at.saturating_sub(now)));
                self.pending_ready = Some(select_ready(
                    &self.stop_rx,
                    &self.important_rx,
                    &self.ordinary_rx,
                    deadline,
                    self.important_streak >= IMPORTANT_BURST,
                ));
            }

            // Waiting may cross an absolute deadline. Recheck before handling
            // the selected message so a ready event cannot outrun that work.
            let dispatch_now = clock_ms();
            let update = controller
                .advance(dispatch_now)
                .map_err(OwnerError::Controller)?;
            publish_update(observer, controller, update)?;
            let update = controller.reconcile().map_err(OwnerError::Controller)?;
            publish_update(observer, controller, update)?;
            if controller.status() == wifi::ProviderStatus::Stopped {
                self.disable_receivers();
                return Ok(());
            }
            if matches!(controller.status(), wifi::ProviderStatus::Failed(_)) && !stop_requested {
                controller.request_stop().map_err(OwnerError::Stop)?;
                stop_requested = true;
            }

            let ready = self
                .pending_ready
                .take()
                .expect("selected owner arm is retained until dispatch");
            match ready {
                Ready::Stop(result) => {
                    if !stop_requested {
                        if let Err(error) = controller.request_stop() {
                            self.pending_ready = Some(Ready::Stop(result));
                            return Err(OwnerError::Stop(error));
                        }
                        stop_requested = true;
                    }
                    self.stop_rx = never();
                }
                Ready::Important(Ok(Important::Command {
                    command,
                    timeout_ms,
                })) => {
                    self.important_streak = self.important_streak.saturating_add(1);
                    let report = match controller.submit(command, dispatch_now, timeout_ms) {
                        Ok(id) => OwnerReport::CommandAccepted(id),
                        Err(rejected) => OwnerReport::CommandRejected(rejected),
                    };
                    publish(observer, report)?;
                }
                Ready::Important(Ok(Important::Event(event))) => {
                    self.important_streak = self.important_streak.saturating_add(1);
                    let update = controller
                        .on_event(event, dispatch_now)
                        .map_err(OwnerError::Controller)?;
                    publish_update(observer, controller, update)?;
                }
                Ready::Important(Err(_)) => self.important_rx = never(),
                Ready::Ordinary(Ok(event)) => {
                    self.important_streak = 0;
                    let update = controller
                        .on_event(event, dispatch_now)
                        .map_err(OwnerError::Controller)?;
                    publish_update(observer, controller, update)?;
                }
                Ready::Ordinary(Err(_)) => self.ordinary_rx = never(),
                Ready::Deadline => {}
            }

            if matches!(controller.status(), wifi::ProviderStatus::Failed(_)) && !stop_requested {
                controller.request_stop().map_err(OwnerError::Stop)?;
                stop_requested = true;
            }
            if controller.status() == wifi::ProviderStatus::Stopped {
                self.disable_receivers();
                return Ok(());
            }
        }
    }

    fn disable_receivers(&mut self) {
        self.stop_rx = never();
        self.important_rx = never();
        self.ordinary_rx = never();
        self.pending_ready = None;
    }
}

fn publish_update<D: wifi::Wifi, O: EventSink<OwnerReport>>(
    observer: &mut O,
    controller: &Controller<D>,
    update: Update,
) -> Result<(), OwnerError> {
    if update == Update::StatusChanged {
        publish(
            observer,
            OwnerReport::State {
                status: controller.status(),
                link: controller.link(),
                fault: controller.fault(),
            },
        )?;
    } else if update != Update::Ignored {
        publish(observer, OwnerReport::Update(update))?;
    }
    Ok(())
}

fn publish<O: EventSink<OwnerReport>>(
    observer: &mut O,
    report: OwnerReport,
) -> Result<(), OwnerError> {
    observer.try_emit(report).map_err(OwnerError::Observer)
}

fn select_ready(
    stop: &Receiver<()>,
    important: &Receiver<Important>,
    ordinary: &Receiver<Event>,
    deadline: Option<Duration>,
    ordinary_first: bool,
) -> Ready {
    match (deadline, ordinary_first) {
        (Some(wait), false) => crossbeam_channel::select_biased! {
            recv(stop) -> value => Ready::Stop(value),
            recv(important) -> value => Ready::Important(value),
            recv(ordinary) -> value => Ready::Ordinary(value),
            default(wait) => Ready::Deadline,
        },
        (Some(wait), true) => crossbeam_channel::select_biased! {
            recv(stop) -> value => Ready::Stop(value),
            recv(ordinary) -> value => Ready::Ordinary(value),
            recv(important) -> value => Ready::Important(value),
            default(wait) => Ready::Deadline,
        },
        (None, false) => crossbeam_channel::select_biased! {
            recv(stop) -> value => Ready::Stop(value),
            recv(important) -> value => Ready::Important(value),
            recv(ordinary) -> value => Ready::Ordinary(value),
        },
        (None, true) => crossbeam_channel::select_biased! {
            recv(stop) -> value => Ready::Stop(value),
            recv(ordinary) -> value => Ready::Ordinary(value),
            recv(important) -> value => Ready::Important(value),
        },
    }
}
