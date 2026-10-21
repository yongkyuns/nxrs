//! Caller-polled reconnect policy for the Wi-Fi controller.
#![no_std]
#![forbid(unsafe_code)]

use wifi::{
    Command, Completion, Controller, ControllerError, Error as WifiError, Event, LinkState,
    ProviderStatus, Update, Wifi,
};

const INITIAL_BACKOFF_MS: u64 = 250;
const MAX_BACKOFF_MS: u64 = 30_000;
const OPERATION_TIMEOUT_MS: u64 = 30_000;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReconnectError {
    Controller(ControllerError),
    Device(WifiError),
    Terminal(WifiError),
    ProviderFault(WifiError),
}

impl From<ControllerError> for ReconnectError {
    fn from(value: ControllerError) -> Self {
        Self::Controller(value)
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ReconnectStats {
    pub attempts: u64,
    pub reconnections: u64,
    pub link_losses: u64,
}

#[derive(Clone, Copy)]
enum StopIntent {
    Disconnect { deadline_ms: u64 },
    Provider,
}

/// Reconnect orchestration without owning a thread, timer, or credentials.
/// The application supplies a fresh connect command for every attempt.
pub struct Reconnector<D, F> {
    controller: Controller<D>,
    command_factory: F,
    stats: ReconnectStats,
    terminal_error: Option<WifiError>,
    provider_fault: Option<WifiError>,
    retry_at_ms: Option<u64>,
    backoff_ms: u64,
    was_addressed: bool,
    awaiting_reconnection: bool,
    reconnect_requested: bool,
    manual_disconnect_done: bool,
    manual_disconnect_inflight: bool,
    stop_intent: Option<StopIntent>,
    shutdown_error: Option<ReconnectError>,
    last_now_ms: u64,
}

impl<D: Wifi, F> Reconnector<D, F>
where
    F: FnMut() -> Result<Command, WifiError>,
{
    pub fn new(controller: Controller<D>, command_factory: F) -> Self {
        let provider_fault = controller.fault();
        Self {
            controller,
            command_factory,
            stats: ReconnectStats::default(),
            terminal_error: None,
            provider_fault,
            retry_at_ms: None,
            backoff_ms: INITIAL_BACKOFF_MS,
            was_addressed: false,
            awaiting_reconnection: false,
            reconnect_requested: false,
            manual_disconnect_done: false,
            manual_disconnect_inflight: false,
            stop_intent: None,
            shutdown_error: None,
            last_now_ms: 0,
        }
    }

    /// Admit an event and update policy state. This does not submit device work.
    pub fn on_event(&mut self, event: Event, now_ms: u64) -> Result<(), ReconnectError> {
        self.check_time(now_ms)?;
        let old_link = self.controller.link();
        let update = self.controller.on_event(event, now_ms)?;
        self.observe_link(old_link, now_ms);
        let result = self.observe_update(update, now_ms);
        self.observe_provider_fault();
        self.report_error().and(result)
    }

    /// Reconcile provider state, advance operation deadlines, and submit at most
    /// one command. Call from the application's normal event loop.
    pub fn poll(&mut self, now_ms: u64) -> Result<(), ReconnectError> {
        self.check_time(now_ms)?;
        let old_link = self.controller.link();
        let update = self.controller.reconcile()?;
        self.observe_link(old_link, now_ms);
        let result = self.observe_update(update, now_ms);
        let update = self.controller.advance(now_ms)?;
        let deadline_result = self.observe_update(update, now_ms);
        self.observe_provider_fault();
        // Shutdown must keep advancing even while a sticky fault is reported.
        if self.stop_intent.is_some() {
            self.advance_shutdown(now_ms)?;
            return self.report_error();
        }
        result?;
        deadline_result?;
        self.report_error()?;

        if self.controller.status() != ProviderStatus::Ready || self.controller.is_busy() {
            return Ok(());
        }

        if self.retry_at_ms.is_some_and(|deadline| now_ms < deadline) {
            return Ok(());
        }
        if self.reconnect_requested {
            if !self.manual_disconnect_done {
                if self.controller.link() == LinkState::Disconnected {
                    self.manual_disconnect_done = true;
                } else {
                    return self.submit_disconnect(now_ms);
                }
            }
            if self.manual_disconnect_done {
                return self.start_connect(now_ms);
            }
        }

        if self.controller.link() == LinkState::Addressed {
            return Ok(());
        }
        self.start_connect(now_ms)
    }

    pub fn link(&self) -> LinkState {
        self.controller.link()
    }

    pub fn status(&self) -> ProviderStatus {
        self.controller.status()
    }

    pub fn stats(&self) -> ReconnectStats {
        self.stats
    }

    pub fn terminal_error(&self) -> Option<WifiError> {
        self.terminal_error
    }

    pub fn provider_fault(&self) -> Option<WifiError> {
        self.provider_fault
    }

    /// First shutdown failure, retained even after provider teardown.
    pub fn shutdown_error(&self) -> Option<ReconnectError> {
        self.shutdown_error
    }

    pub fn next_deadline_ms(&self) -> Option<u64> {
        if let Some(StopIntent::Disconnect { deadline_ms }) = self.stop_intent {
            return Some(
                self.controller
                    .next_deadline_ms()
                    .map_or(deadline_ms, |operation| operation.min(deadline_ms)),
            );
        }
        // A timed-out native operation still owns the slot. Only its provider
        // completion can release it; an expired retry is not another wakeup.
        if self.controller.is_busy() {
            return self.controller.next_deadline_ms();
        }
        match (self.controller.next_deadline_ms(), self.retry_at_ms) {
            (Some(operation), Some(retry)) => Some(operation.min(retry)),
            (Some(operation), None) => Some(operation),
            (None, Some(retry)) => Some(retry),
            (None, None) => None,
        }
    }

    /// Request a disconnect followed by a fresh join. Work starts on `poll`.
    pub fn request_reconnect(&mut self, now_ms: u64) -> Result<(), ReconnectError> {
        self.check_time(now_ms)?;
        if self.stop_intent.is_some() {
            return self.report_error();
        }
        self.reconnect_requested = true;
        self.manual_disconnect_done = false;
        self.retry_at_ms = None;
        self.report_error()
    }

    /// Explicitly authorize disassociation of an owned, exclusively controlled
    /// link, then stop this provider. Call from the owning event loop and keep
    /// driving events and polling even if an error is returned.
    ///
    /// Reuses any pending manual disconnect; otherwise submits at most one.
    /// After 30 seconds, request provider stop even if native work is pending,
    /// retaining Timeout rather than claiming a clean disconnect. Call finish
    /// only after Stopped to join the provider and retrieve retained failures.
    /// Request this before provider stop; stopping cannot disassociate a link.
    pub fn request_disconnect_and_stop(&mut self, now_ms: u64) -> Result<(), ReconnectError> {
        self.check_time(now_ms)?;
        if self.stop_intent.is_none() {
            self.stop_intent = Some(StopIntent::Disconnect {
                deadline_ms: now_ms.saturating_add(OPERATION_TIMEOUT_MS),
            });
            self.retry_at_ms = None;
            self.reconnect_requested = false;
            self.manual_disconnect_done = false;
        }
        self.poll(now_ms)
    }

    /// Immediately stop the provider without implicitly disassociating.
    /// Failed stop admission is retained and retried by subsequent polls.
    pub fn request_stop(&mut self) -> Result<(), ReconnectError> {
        if matches!(self.stop_intent, Some(StopIntent::Disconnect { .. })) && !self.disconnect_done()
        {
            self.shutdown_error
                .get_or_insert(ReconnectError::Device(WifiError::Unavailable));
        }
        self.stop_intent = Some(StopIntent::Provider);
        self.retry_at_ms = None;
        self.reconnect_requested = false;
        let result = self
            .controller
            .request_stop()
            .map_err(ReconnectError::Device);
        if let Err(error) = result {
            self.shutdown_error.get_or_insert(error);
        }
        result
    }

    /// Teardown only after provider status is Stopped and callbacks are quiet.
    /// Joins before returning any retained shutdown or terminal failure.
    pub fn finish(&mut self) -> Result<(), ReconnectError> {
        self.controller.finish().map_err(ReconnectError::Device)?;
        self.report_error()
    }

    fn advance_shutdown(&mut self, now_ms: u64) -> Result<(), ReconnectError> {
        let Some(StopIntent::Disconnect { deadline_ms }) = self.stop_intent else {
            return self.request_stop();
        };
        if self.controller.status() != ProviderStatus::Ready
            || self.provider_fault.is_some()
            || self.shutdown_error.is_some()
        {
            return self.request_stop();
        }
        if self.disconnect_done() {
            return self.request_stop();
        }
        if now_ms >= deadline_ms {
            self.shutdown_error
                .get_or_insert(ReconnectError::Device(WifiError::Timeout));
            return self.request_stop();
        }
        if !self.controller.is_busy() {
            // Rejected disconnects are failures too: do not retry disassociation.
            if let Err(error) = self.submit_disconnect(now_ms) {
                self.shutdown_error.get_or_insert(error);
                return self.request_stop();
            }
        }
        Ok(())
    }

    fn disconnect_done(&self) -> bool {
        !self.controller.is_busy()
            && (self.manual_disconnect_done
                || matches!(
                    self.controller.link(),
                    LinkState::Disconnected | LinkState::Unknown
                ))
    }

    fn check_time(&mut self, now_ms: u64) -> Result<(), ReconnectError> {
        if now_ms < self.last_now_ms {
            return Err(ReconnectError::Controller(
                ControllerError::ClockWentBackwards,
            ));
        }
        self.last_now_ms = now_ms;
        Ok(())
    }

    fn observe_link(&mut self, old_link: LinkState, now_ms: u64) {
        let link = self.controller.link();
        if link == LinkState::Addressed {
            if !self.was_addressed {
                self.backoff_ms = INITIAL_BACKOFF_MS;
                self.retry_at_ms = None;
                self.was_addressed = true;
            }
            if self.awaiting_reconnection {
                self.stats.reconnections = self.stats.reconnections.saturating_add(1);
                self.awaiting_reconnection = false;
                self.backoff_ms = INITIAL_BACKOFF_MS;
            }
        } else if old_link == LinkState::Addressed && link != LinkState::Addressed {
            self.stats.link_losses = self.stats.link_losses.saturating_add(1);
            self.awaiting_reconnection = true;
            if self.reconnect_requested {
                self.manual_disconnect_done = true;
            } else {
                self.schedule_retry(now_ms);
            }
        } else if self.reconnect_requested && link == LinkState::Disconnected {
            self.manual_disconnect_done = true;
        }
    }

    fn observe_update(&mut self, update: Update, now_ms: u64) -> Result<(), ReconnectError> {
        if let Update::Finished(outcome) = update {
            match outcome.result {
                Ok(Completion::Connected) if self.controller.link() != LinkState::Addressed => {
                    self.schedule_retry(now_ms);
                }
                Ok(Completion::Disconnected)
                    if self.reconnect_requested
                        || matches!(self.stop_intent, Some(StopIntent::Disconnect { .. })) =>
                {
                    self.manual_disconnect_inflight = false;
                    self.manual_disconnect_done = true;
                }
                Err(error) if matches!(self.stop_intent, Some(StopIntent::Disconnect { .. })) => {
                    self.manual_disconnect_inflight = false;
                    let error = ReconnectError::Device(error);
                    self.shutdown_error.get_or_insert(error);
                    return Err(error);
                }
                Err(error) if self.manual_disconnect_inflight => {
                    self.manual_disconnect_inflight = false;
                    if self.controller.link() == LinkState::Disconnected {
                        self.manual_disconnect_done = true;
                    }
                    self.handle_error(error, now_ms)?;
                }
                Err(error) => {
                    self.handle_error(error, now_ms)?;
                }
                _ => {}
            }
        }
        Ok(())
    }

    fn submit_disconnect(&mut self, now_ms: u64) -> Result<(), ReconnectError> {
        match self
            .controller
            .submit(Command::Disconnect, now_ms, OPERATION_TIMEOUT_MS)
        {
            Ok(_) => {
                self.manual_disconnect_inflight = true;
                Ok(())
            }
            Err(rejected) if matches!(self.stop_intent, Some(StopIntent::Disconnect { .. })) => {
                Err(match rejected.error {
                    ControllerError::Device(error) => ReconnectError::Device(error),
                    other => ReconnectError::Controller(other),
                })
            }
            Err(rejected) => self.handle_submit_error(rejected.error, now_ms),
        }
    }

    fn start_connect(&mut self, now_ms: u64) -> Result<(), ReconnectError> {
        let command = match (self.command_factory)() {
            Ok(command @ Command::Connect { .. }) => command,
            Ok(_) => return self.set_terminal(WifiError::InvalidConfig),
            Err(error) => return self.handle_error(error, now_ms),
        };
        match self.controller.submit(command, now_ms, OPERATION_TIMEOUT_MS) {
            Ok(_) => {
                self.stats.attempts = self.stats.attempts.saturating_add(1);
                self.retry_at_ms = None;
                self.reconnect_requested = false;
                self.manual_disconnect_done = false;
                self.manual_disconnect_inflight = false;
                Ok(())
            }
            Err(rejected) => self.handle_submit_error(rejected.error, now_ms),
        }
    }

    fn handle_submit_error(
        &mut self,
        error: ControllerError,
        now_ms: u64,
    ) -> Result<(), ReconnectError> {
        match error {
            ControllerError::Device(error) => self.handle_error(error, now_ms),
            other => Err(ReconnectError::Controller(other)),
        }
    }

    fn handle_error(&mut self, error: WifiError, now_ms: u64) -> Result<(), ReconnectError> {
        if is_terminal(error) {
            self.set_terminal(error)
        } else {
            self.schedule_retry(now_ms);
            Ok(())
        }
    }

    fn set_terminal(&mut self, error: WifiError) -> Result<(), ReconnectError> {
        self.terminal_error.get_or_insert(error);
        self.retry_at_ms = None;
        self.report_error()
    }

    fn report_error(&self) -> Result<(), ReconnectError> {
        if let Some(error) = self.provider_fault {
            return Err(ReconnectError::ProviderFault(error));
        }
        if let Some(error) = self.shutdown_error {
            return Err(error);
        }
        match self.terminal_error {
            Some(error) => Err(ReconnectError::Terminal(error)),
            None => Ok(()),
        }
    }

    fn observe_provider_fault(&mut self) {
        if let Some(error) = self.controller.fault() {
            self.provider_fault.get_or_insert(error);
            self.retry_at_ms = None;
        }
    }

    fn schedule_retry(&mut self, now_ms: u64) {
        if self.stop_intent.is_some()
            || self.provider_fault.is_some()
            || self.terminal_error.is_some()
        {
            return;
        }
        self.retry_at_ms = Some(now_ms.saturating_add(self.backoff_ms));
        self.backoff_ms = self.backoff_ms.saturating_mul(2).min(MAX_BACKOFF_MS);
    }
}

fn is_terminal(error: WifiError) -> bool {
    matches!(
        error,
        WifiError::AuthenticationFailed
            | WifiError::InvalidConfig
            | WifiError::PermissionDenied
            | WifiError::Unsupported
            | WifiError::Protocol
    )
}
