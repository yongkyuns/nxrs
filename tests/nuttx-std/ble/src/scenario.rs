//! Shared portable GATT qualification against the radio fixture.
#![forbid(unsafe_code)]

use ble::Uuid;

/// Immutable V2 qualification service; V1 remains available for compatibility.
pub const SERVICE: Uuid = Uuid([
    0x4e, 0x58, 0x52, 0x53, 0x00, 0x02, 0x40, 0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01,
]);
const LEGACY_SERVICE: Uuid = Uuid([
    0x4e, 0x58, 0x52, 0x53, 0x00, 0x01, 0x40, 0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01,
]);
const CHARACTERISTIC: Uuid = Uuid([
    0x4e, 0x58, 0x52, 0x53, 0x00, 0x01, 0x40, 0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x02,
]);
const VALUE_LEN: usize = 20;
pub const CAPACITY: usize = 64;
const ROUNDS: u32 = 100;

fn scan_cleanup_should_wait(
    status: ble::ProviderStatus,
    now: u64,
    deadline: u64,
) -> Result<bool, &'static str> {
    if status == ble::ProviderStatus::Stopped {
        Ok(false)
    } else if now >= deadline {
        Err("scan_shutdown_timeout")
    } else {
        Ok(true)
    }
}

fn admission_failure(error: ble::ControllerError, pending: bool) -> &'static str {
    match error {
        ble::ControllerError::Device(ble::Error::Busy) if pending => "gatt_pending",
        ble::ControllerError::Device(ble::Error::Busy) => "gatt_backend_busy",
        ble::ControllerError::Device(ble::Error::InvalidValue) => "gatt_invalid",
        ble::ControllerError::Device(ble::Error::Unavailable) => "gatt_unavailable",
        _ => "gatt_admission",
    }
}

fn unexpected_disconnect(state: ble::gatt::State) -> bool {
    // A requested disconnect can report link loss before its terminal callback
    // barrier. Keep waiting; only completion or the original deadline ends it.
    state != ble::gatt::State::Disconnecting
}

enum Admission<const N: usize> {
    Accepted(ble::RequestId),
    Retry(ble::gatt::Command<N>),
}

/// Rejected work stays owned and admission time counts against the original
/// operation deadline. An already-pending request is a caller bug, not retry.
fn attempt_admission<const N: usize, D: ble::gatt::Driver<N>>(
    client: &mut ble::gatt::Client<D, N>,
    command: ble::gatt::Command<N>,
    now: u64,
    deadline: u64,
) -> Result<Admission<N>, &'static str> {
    if now >= deadline {
        return Err("gatt_admission_timeout");
    }
    let pending = client.is_busy();
    match client.submit(command, now, deadline - now) {
        Ok(id) => Ok(Admission::Accepted(id)),
        Err(rejected)
            if !pending && rejected.error == ble::ControllerError::Device(ble::Error::Busy) =>
        {
            Ok(Admission::Retry(rejected.value))
        }
        Err(rejected) => Err(admission_failure(rejected.error, pending)),
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Full,
    Cancel,
    Values,
    Legacy,
}

impl Mode {
    /// Both firmware entries accept exactly one optional mode argument.
    pub fn from_args(mut args: impl Iterator<Item = String>) -> Option<Self> {
        let mode = Self::parse(args.next().as_deref())?;
        args.next().is_none().then_some(mode)
    }

    pub fn parse(value: Option<&str>) -> Option<Self> {
        match value {
            None => Some(Self::Full),
            Some("cancel") => Some(Self::Cancel),
            Some("values") => Some(Self::Values),
            Some("legacy") => Some(Self::Legacy),
            _ => None,
        }
    }

    fn service(self) -> Uuid {
        if self == Self::Legacy {
            LEGACY_SERVICE
        } else {
            SERVICE
        }
    }
}

/// The owner may drain other nonblocking event sources at the same wait point.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Progress {
    Poll,
    FirstWrite { elapsed_ms: u64 },
    LastRead { elapsed_ms: u64, pairs: u32 },
}

/// Run the standalone workload with nonblocking hooks. A hook failure fails
/// qualification but never prevents callback retirement or worker cleanup.
#[cfg(target_os = "nuttx")]
pub fn run(mode: Mode, mut observer: impl FnMut(Progress) -> Result<(), &'static str>) -> bool {
    device::run_with(mode, &mut observer, ble::open, |wake, recovery_ms| {
        ble::gatt::open::<CAPACITY>(move || wake.notify(), recovery_ms)
    })
}

pub use device::{run_with, ScanSink, WakeContext};

fn payload(sequence: u32) -> [u8; VALUE_LEN] {
    let mut value = [0; VALUE_LEN];
    value[..4].copy_from_slice(&sequence.to_be_bytes());
    for (index, byte) in value[4..].iter_mut().enumerate() {
        *byte = sequence.wrapping_add(index as u32) as u8;
    }
    value
}

fn validation_value_matches(bytes: &[u8], length: usize) -> bool {
    bytes.len() == length
        && bytes
            .iter()
            .enumerate()
            .all(|(index, byte)| *byte == length.wrapping_add(index) as u8)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum PairStep {
    Write(u32),
    Read(u32),
    Done,
}

fn next_pair_step(step: PairStep, rounds: u32) -> PairStep {
    match step {
        PairStep::Write(sequence) => PairStep::Read(sequence),
        PairStep::Read(sequence) if sequence + 1 < rounds => PairStep::Write(sequence + 1),
        PairStep::Read(_) | PairStep::Done => PairStep::Done,
    }
}

mod device {
    use super::{
        next_pair_step, payload, validation_value_matches, Mode, PairStep, Progress, CAPACITY,
        CHARACTERISTIC, ROUNDS, VALUE_LEN,
    };
    use ble::{
        gatt::{
            Attribute, Client, Command as GattCommand, Completion as GattCompletion, Driver,
            Failure, Peer, State as GattState, Update as GattUpdate,
        },
        Ble, Command, Completion, Controller, DeliveryError, Error, Event, EventKind, EventSink,
        ProviderStatus, Rejected, Update,
    };
    use std::{
        sync::mpsc::{self, Receiver, SyncSender, TryRecvError, TrySendError},
        sync::{
            atomic::{AtomicBool, Ordering},
            Arc,
        },
        thread::{self, Thread},
        time::{Duration, Instant},
    };

    const SCAN_MS: u64 = 5_000;
    const STARTUP_MS: u64 = 5_000;
    const CONNECT_MS: u64 = 15_000;
    const OPERATION_MS: u64 = 5_000;
    const DISCONNECT_MS: u64 = 15_000;
    const RECOVERY_MS: u64 = 5_000;
    const TOTAL_MS: u64 = 50_000;
    const CLEANUP_MS: u64 = 5_000;
    const SCAN_QUEUE: usize = 8;

    pub struct WakeContext {
        owner: Thread,
        released: Arc<AtomicBool>,
    }

    impl WakeContext {
        pub fn notify(&self) {
            self.owner.unpark();
        }
    }

    impl Drop for WakeContext {
        fn drop(&mut self) {
            self.released.store(true, Ordering::Release);
            self.owner.unpark();
        }
    }

    pub struct ScanSink {
        sender: SyncSender<Event>,
        owner: Thread,
        overflowed: Arc<AtomicBool>,
    }

    impl EventSink<Event> for ScanSink {
        fn try_emit(&mut self, event: Event) -> Result<(), Rejected<Event, DeliveryError>> {
            self.sender.try_send(event).map_err(|error| match error {
                TrySendError::Full(value) => {
                    self.overflowed.store(true, Ordering::Release);
                    Rejected {
                        value,
                        error: DeliveryError::Full,
                    }
                }
                TrySendError::Disconnected(value) => Rejected {
                    value,
                    error: DeliveryError::Closed,
                },
            })?;
            self.owner.unpark();
            Ok(())
        }
    }

    struct Runtime<'a, S, D> {
        service: ble::Uuid,
        scan: Controller<S>,
        gatt: Client<D, CAPACITY>,
        events: Receiver<Event>,
        epoch: Instant,
        first_peer: Option<ble::PeerId>,
        ambiguous: bool,
        discovery_limited: bool,
        scan_started: bool,
        scan_stopped: bool,
        scan_failure: Option<Error>,
        scan_overflowed: Arc<AtomicBool>,
        finished: Option<GattUpdate<CAPACITY>>,
        recovery: Option<GattUpdate<CAPACITY>>,
        link_lost: bool,
        overall_deadline: u64,
        observer: Option<&'a mut dyn FnMut(Progress) -> Result<(), &'static str>>,
    }

    impl<S: Ble, D: Driver<CAPACITY>> Runtime<'_, S, D> {
        fn now(&self) -> u64 {
            self.epoch.elapsed().as_millis() as u64
        }

        fn pump(&mut self) -> Result<(), &'static str> {
            self.report(Progress::Poll)?;
            loop {
                let mut progress = false;
                loop {
                    match self.events.try_recv() {
                        Ok(event) => {
                            progress = true;
                            match self
                                .scan
                                .on_event(event, self.now())
                                .map_err(|_| "scan_clock")?
                            {
                                Update::Event(EventKind::Discovered { peer, .. }) => {
                                    match self.first_peer {
                                        None => self.first_peer = Some(peer),
                                        Some(first) if first != peer => self.ambiguous = true,
                                        _ => {}
                                    }
                                }
                                Update::Event(EventKind::DiscoveryLimited { .. }) => {
                                    self.discovery_limited = true
                                }
                                Update::Finished(outcome) => match outcome.result {
                                    Ok(Completion::ScanStarted) => self.scan_started = true,
                                    Ok(Completion::ScanStopped) => self.scan_stopped = true,
                                    Err(error) => self.scan_failure = Some(error),
                                },
                                _ => {}
                            }
                        }
                        Err(TryRecvError::Empty) => break,
                        Err(TryRecvError::Disconnected) => return Err("scan_events_closed"),
                    }
                }
                if let Update::Finished(outcome) =
                    self.scan.advance(self.now()).map_err(|_| "scan_clock")?
                {
                    match outcome.result {
                        Ok(Completion::ScanStarted) => self.scan_started = true,
                        Ok(Completion::ScanStopped) => self.scan_stopped = true,
                        Err(error) => self.scan_failure = Some(error),
                    }
                }
                loop {
                    match self.gatt.poll(self.now()).map_err(|_| "gatt_clock")? {
                        GattUpdate::None => break,
                        GattUpdate::Finished(outcome) => {
                            progress = true;
                            self.finished = Some(GattUpdate::Finished(outcome));
                        }
                        update @ (GattUpdate::Recovered
                        | GattUpdate::RecoveryTimedOut
                        | GattUpdate::Released) => {
                            progress = true;
                            self.recovery = Some(update);
                        }
                        GattUpdate::Disconnected { .. } => {
                            progress = true;
                            self.link_lost |= super::unexpected_disconnect(self.gatt.state());
                        }
                        GattUpdate::Ignored => {}
                    }
                }
                self.scan.reconcile().map_err(|_| "scan_reconcile")?;
                if self.scan_failure.is_none() {
                    self.scan_failure = self.scan.fault();
                }
                if !progress {
                    break;
                }
            }
            Ok(())
        }

        fn report(&mut self, progress: Progress) -> Result<(), &'static str> {
            self.observer
                .as_mut()
                .map_or(Ok(()), |observer| observer(progress))
        }

        fn take_update(&mut self, id: ble::RequestId) -> Option<GattUpdate<CAPACITY>> {
            if matches!(self.finished, Some(GattUpdate::Finished(outcome)) if outcome.id == id) {
                self.finished.take()
            } else {
                None
            }
        }

        fn wait_for_gatt(
            &mut self,
            id: ble::RequestId,
        ) -> Result<GattCompletion<CAPACITY>, &'static str> {
            match self.wait_for_outcome(id)? {
                Ok(completion) => Ok(completion),
                Err(failure) => {
                    let reason = gatt_failure(failure);
                    if self.gatt.state() == GattState::Recovering {
                        self.wait_for_recovery()?;
                    }
                    Err(reason)
                }
            }
        }

        fn wait_for_outcome(
            &mut self,
            id: ble::RequestId,
        ) -> Result<Result<GattCompletion<CAPACITY>, Failure>, &'static str> {
            loop {
                self.pump()?;
                if let Some(GattUpdate::Finished(outcome)) = self.take_update(id) {
                    return Ok(outcome.result);
                }
                if self.link_lost {
                    return Err("link_lost");
                }
                if self.now() >= self.overall_deadline {
                    return Err("overall_timeout");
                }
                self.wait_until(self.overall_deadline);
            }
        }

        fn wait_for_recovery(&mut self) -> Result<(), &'static str> {
            loop {
                self.pump()?;
                if let Some(update) = self.recovery.take() {
                    return match update {
                        GattUpdate::Recovered => Ok(()),
                        GattUpdate::RecoveryTimedOut => Err("recovery_timeout"),
                        _ => Err("recovery_barrier_missing"),
                    };
                }
                if self.now() >= self.overall_deadline {
                    return Err("overall_timeout");
                }
                self.wait_until(self.overall_deadline);
            }
        }

        fn wait_until(&mut self, deadline: u64) {
            let now = self.now();
            let next = self
                .gatt
                .next_deadline_ms()
                .into_iter()
                .chain(self.scan.next_deadline_ms())
                .chain(Some(deadline))
                .min()
                .unwrap_or(deadline);
            if next > now {
                park(Duration::from_millis(next - now));
            }
        }

        fn gatt_submit(
            &mut self,
            mut command: GattCommand<CAPACITY>,
            timeout: u64,
        ) -> Result<ble::RequestId, &'static str> {
            let deadline = self
                .now()
                .checked_add(timeout)
                .filter(|_| timeout != 0)
                .ok_or("gatt_admission")?;
            loop {
                let now = self.now();
                match super::attempt_admission(&mut self.gatt, command, now, deadline)? {
                    super::Admission::Accepted(id) => return Ok(id),
                    super::Admission::Retry(returned) => command = returned,
                }
                self.pump()?;
                // Backend readiness shares the existing owner wakeup. No
                // spin, random delay, extra queue or deadline extension.
                self.wait_until(deadline);
            }
        }

        fn connect(&mut self, peer: Peer) -> Result<(), &'static str> {
            let id = self.gatt_submit(GattCommand::Connect { peer }, CONNECT_MS)?;
            if !matches!(self.wait_for_gatt(id)?, GattCompletion::Connected(_)) {
                return Err("connect_protocol");
            }
            Ok(())
        }

        fn discover(&mut self) -> Result<Attribute, &'static str> {
            self.discover_uuid(CHARACTERISTIC)
        }

        fn discover_uuid(&mut self, characteristic: ble::Uuid) -> Result<Attribute, &'static str> {
            let id = self.gatt_submit(
                GattCommand::Discover {
                    service: self.service,
                    characteristic,
                },
                OPERATION_MS,
            )?;
            match self.wait_for_gatt(id)? {
                GattCompletion::Discovered(attribute) => Ok(attribute),
                _ => Err("discover_protocol"),
            }
        }

        fn read(&mut self, attribute: Attribute) -> Result<[u8; VALUE_LEN], &'static str> {
            let id = self.gatt_submit(GattCommand::Read { attribute }, OPERATION_MS)?;
            match self.wait_for_gatt(id)? {
                GattCompletion::Read(value) => {
                    if value.as_slice().len() != VALUE_LEN {
                        return Err("read_length");
                    }
                    let mut bytes = [0; VALUE_LEN];
                    bytes.copy_from_slice(value.as_slice());
                    Ok(bytes)
                }
                _ => Err("read_protocol"),
            }
        }

        fn write(
            &mut self,
            attribute: Attribute,
            bytes: &[u8; VALUE_LEN],
        ) -> Result<(), &'static str> {
            let value = ble::gatt::Value::new(bytes).ok_or("value_size")?;
            let id = self.gatt_submit(GattCommand::Write { attribute, value }, OPERATION_MS)?;
            if !matches!(self.wait_for_gatt(id)?, GattCompletion::Written) {
                return Err("write_protocol");
            }
            Ok(())
        }

        fn check_remote_error(&mut self, attribute: Attribute) -> Result<(), &'static str> {
            // The echo value is unavailable until its first write. A remote ATT
            // failure must not retire an otherwise usable connection.
            let id = self.gatt_submit(GattCommand::Read { attribute }, OPERATION_MS)?;
            if self.wait_for_outcome(id)? != Err(Failure::Att(0x0e))
                || !matches!(self.gatt.state(), GattState::Connected(_))
            {
                return Err("remote_error_contract");
            }
            println!("NXRS_GATT_VALUES check=remote_error att=14 link=preserved");
            Ok(())
        }

        fn check_values(&mut self) -> Result<(), &'static str> {
            for (last, length) in [(3, 44), (4, CAPACITY)] {
                let mut uuid = CHARACTERISTIC;
                uuid.0[15] = last;
                let attribute = self.discover_uuid(uuid)?;
                let id = self.gatt_submit(GattCommand::Read { attribute }, OPERATION_MS)?;
                match self.wait_for_gatt(id)? {
                    GattCompletion::Read(value)
                        if validation_value_matches(value.as_slice(), length) => {}
                    _ => return Err("long_value_mismatch"),
                }
                println!("NXRS_GATT_VALUES check=complete_read bytes={length}");
            }

            let mut uuid = CHARACTERISTIC;
            uuid.0[15] = 5;
            let attribute = self.discover_uuid(uuid)?;
            let id = self.gatt_submit(GattCommand::Read { attribute }, OPERATION_MS)?;
            if self.wait_for_outcome(id)? != Err(Failure::Device(Error::InvalidValue)) {
                return Err("oversized_value_not_rejected");
            }
            // A local transfer failure retires the link. A prefix, a disconnect
            // alone, or simply dropping the owner is not a recovery barrier.
            self.wait_for_recovery()?;
            if self.gatt.state() != GattState::Idle {
                return Err("oversized_value_not_quiescent");
            }
            println!("NXRS_GATT_VALUES check=oversized_read bytes=65 capacity={CAPACITY} outcome=rejected barrier=quiescent");
            Ok(())
        }

        fn disconnect(&mut self) -> Result<(), &'static str> {
            let id = self.gatt_submit(GattCommand::Disconnect, DISCONNECT_MS)?;
            if !matches!(self.wait_for_gatt(id)?, GattCompletion::Disconnected) {
                return Err("disconnect_protocol");
            }
            Ok(())
        }
    }

    pub(super) fn gatt_failure(failure: Failure) -> &'static str {
        match failure {
            Failure::Device(error) => match error {
                Error::Unsupported => "operation_unsupported",
                Error::Busy => "operation_busy",
                Error::Unavailable => "operation_unavailable",
                Error::PermissionDenied => "operation_denied",
                Error::InvalidValue => "operation_invalid",
                Error::Timeout => "operation_timeout",
                Error::Io => "operation_io",
            },
            Failure::Disconnected => "link_lost",
            Failure::Cancelled => "cancelled",
            Failure::Att(_) => "att_error",
            Failure::Remote(_) => "remote_error",
            Failure::Protocol => "protocol_error",
        }
    }

    /// Same workload and lifecycle oracle on every OS. The executable injects
    /// platform-selected acquisition; this module cannot silently use a mock.
    pub fn run_with<S: Ble, D: Driver<CAPACITY>>(
        mode: Mode,
        observer: &mut dyn FnMut(Progress) -> Result<(), &'static str>,
        open_scan: impl FnOnce(ScanSink) -> Result<Controller<S>, Error>,
        open_gatt: impl FnOnce(WakeContext, u64) -> Result<Client<D, CAPACITY>, Error>,
    ) -> bool {
        let owner = thread::current();
        let released = Arc::new(AtomicBool::new(false));
        let wake_context = WakeContext {
            owner: owner.clone(),
            released: released.clone(),
        };
        let gatt = match open_gatt(wake_context, RECOVERY_MS) {
            Ok(gatt) => gatt,
            Err(_) => {
                println!("NXRS_GATT_CLIENT result=fail stage=gatt_open cleanup_gatt=not_open cleanup_scan=not_open native_close=not_proven");
                return false;
            }
        };
        let (sender, events) = mpsc::sync_channel(SCAN_QUEUE);
        let scan_overflowed = Arc::new(AtomicBool::new(false));
        let scan = match open_scan(ScanSink {
            sender,
            owner: owner.clone(),
            overflowed: scan_overflowed.clone(),
        }) {
            Ok(scan) => scan,
            Err(_) => {
                drop(gatt);
                let native_close = wait_for_native_release(
                    &released,
                    Instant::now() + Duration::from_millis(CLEANUP_MS),
                )
                .is_ok();
                println!("NXRS_GATT_CLIENT result=fail stage=scan_open cleanup_gatt=not_needed cleanup_scan=failed native_close={}", flag(native_close));
                return false;
            }
        };
        let mut runtime = Runtime {
            service: mode.service(),
            scan,
            gatt,
            events,
            epoch: Instant::now(),
            first_peer: None,
            ambiguous: false,
            discovery_limited: false,
            scan_started: false,
            scan_stopped: false,
            scan_failure: None,
            scan_overflowed,
            finished: None,
            recovery: None,
            link_lost: false,
            overall_deadline: TOTAL_MS,
            observer: Some(observer),
        };

        let qualification = qualify(&mut runtime, mode);
        // A persistent background fault must not starve callback retirement.
        runtime.observer = None;
        runtime.overall_deadline = runtime.now().saturating_add(CLEANUP_MS);
        let gatt_cleanup = cleanup_gatt(&mut runtime);
        let scan_cleanup = cleanup_scan(&mut runtime);
        drop(runtime);
        let cleanup_deadline = Instant::now() + Duration::from_millis(CLEANUP_MS);
        let native_cleanup = wait_for_native_release(&released, cleanup_deadline);
        let cleanup_ok = gatt_cleanup.is_ok() && scan_cleanup.is_ok() && native_cleanup.is_ok();
        match qualification {
            Ok(pairs) if cleanup_ok => {
                if mode == Mode::Cancel {
                    println!("NXRS_GATT_CLIENT result=pass mode=cancel cleanup_gatt=confirmed cleanup_scan=confirmed native_close=confirmed");
                } else if mode == Mode::Values {
                    println!("NXRS_GATT_CLIENT result=pass mode=values pairs={pairs} bytes_per_pair={VALUE_LEN} complete_values=44,64 oversized=65_rejected remote_error=preserved cleanup_gatt=confirmed cleanup_scan=confirmed native_close=confirmed");
                } else if mode == Mode::Legacy {
                    println!("NXRS_GATT_CLIENT result=pass mode=legacy pairs={pairs} bytes_per_pair={VALUE_LEN} cleanup_gatt=confirmed cleanup_scan=confirmed native_close=confirmed");
                } else {
                    println!("NXRS_GATT_CLIENT result=pass pairs={pairs} bytes_per_pair={VALUE_LEN} cleanup_gatt=confirmed cleanup_scan=confirmed native_close=confirmed next=rerun_after_fixture_restart");
                }
                true
            }
            result => {
                let stage = result
                    .err()
                    .or_else(|| gatt_cleanup.err())
                    .or_else(|| scan_cleanup.err())
                    .or_else(|| native_cleanup.err())
                    .unwrap_or("cleanup_failed");
                println!("NXRS_GATT_CLIENT result=fail stage={stage} cleanup_gatt={} cleanup_scan={} native_close={}", flag(gatt_cleanup.is_ok()), flag(scan_cleanup.is_ok()), flag(native_cleanup.is_ok()));
                false
            }
        }
    }

    fn qualify<S: Ble, D: Driver<CAPACITY>>(
        runtime: &mut Runtime<'_, S, D>,
        mode: Mode,
    ) -> Result<u32, &'static str> {
        println!("NXRS_GATT_CLIENT phase=start");
        let startup_deadline = STARTUP_MS;
        loop {
            runtime.pump()?;
            if runtime.scan.status() == ProviderStatus::Ready {
                break;
            }
            if runtime.scan.fault().is_some() {
                return Err("scan_startup_failed");
            }
            if runtime.now() >= startup_deadline {
                return Err("startup_timeout");
            }
            runtime.wait_until(startup_deadline);
        }

        runtime
            .scan
            .submit(
                Command::StartScan {
                    service: Some(runtime.service),
                },
                runtime.now(),
                STARTUP_MS,
            )
            .map_err(|_| "scan_admission")?;
        let scan_start_deadline = runtime.now().saturating_add(STARTUP_MS);
        loop {
            runtime.pump()?;
            if runtime.scan_failure.is_some() {
                return Err("scan_failed");
            }
            if runtime.scan_started {
                break;
            }
            if runtime.now() >= scan_start_deadline {
                return Err("scan_start_timeout");
            }
            runtime.wait_until(scan_start_deadline);
        }
        println!("NXRS_GATT_CLIENT phase=scan_started window_ms={SCAN_MS} fixture_window_independent=true");
        let scan_end = runtime.now().saturating_add(SCAN_MS);
        while runtime.now() < scan_end {
            runtime.pump()?;
            if runtime.scan_failure.is_some() {
                return Err("scan_failed");
            }
            runtime.wait_until(scan_end);
        }
        runtime
            .scan
            .submit(Command::StopScan, runtime.now(), STARTUP_MS)
            .map_err(|_| "scan_stop_admission")?;
        loop {
            runtime.pump()?;
            if runtime.scan_failure.is_some() {
                return Err("scan_failed");
            }
            if runtime.scan_stopped {
                break;
            }
            if runtime.now() >= runtime.overall_deadline {
                return Err("overall_timeout");
            }
            runtime.wait_until(runtime.overall_deadline);
        }
        runtime.pump()?;
        if runtime.scan_overflowed.load(Ordering::Acquire) {
            return Err("scan_event_overflow");
        }
        if runtime.discovery_limited {
            return Err("scan_results_limited");
        }
        if runtime.ambiguous {
            return Err("peer_ambiguous");
        }
        let Some(peer_id) = runtime.first_peer else {
            return Err("peer_missing");
        };
        let peer = Peer {
            session: runtime.scan.session(),
            id: peer_id,
        };
        println!("NXRS_GATT_CLIENT phase=scan_stopped peers=1");

        runtime.connect(peer)?;
        println!("NXRS_GATT_CLIENT phase=connected");

        if mode == Mode::Cancel {
            let id = runtime.gatt_submit(
                GattCommand::Discover {
                    service: runtime.service,
                    characteristic: CHARACTERISTIC,
                },
                OPERATION_MS,
            )?;
            let cancellation = runtime
                .gatt
                .cancel(runtime.now())
                .map_err(|_| "cancel_clock")?;
            if !matches!(cancellation, GattUpdate::Finished(outcome) if outcome.id == id && outcome.result == Err(Failure::Cancelled))
            {
                return Err("cancel_outcome");
            }
            println!("NXRS_GATT_CLIENT phase=cancel_requested outcome=cancelled");
            runtime.wait_for_recovery()?;
            if runtime.gatt.state() != GattState::Idle {
                return Err("cancel_not_quiescent");
            }
            println!("NXRS_GATT_CLIENT phase=recovered barrier=quiescent");
            return Ok(0);
        }

        let attribute = runtime.discover()?;
        println!("NXRS_GATT_CLIENT phase=discovered");
        if mode == Mode::Values {
            runtime.check_remote_error(attribute)?;
        }

        let mut step = PairStep::Write(0);
        let mut completed = 0;
        while let PairStep::Write(sequence) | PairStep::Read(sequence) = step {
            match step {
                PairStep::Write(_) => {
                    runtime.write(attribute, &payload(sequence))?;
                    if sequence == 0 {
                        runtime.report(Progress::FirstWrite {
                            elapsed_ms: runtime.now(),
                        })?;
                    }
                }
                PairStep::Read(_) => {
                    if runtime.read(attribute)? != payload(sequence) {
                        return Err("payload_mismatch");
                    }
                    completed += 1;
                    if completed == ROUNDS {
                        runtime.report(Progress::LastRead {
                            elapsed_ms: runtime.now(),
                            pairs: completed,
                        })?;
                    }
                }
                PairStep::Done => unreachable!(),
            }
            step = next_pair_step(step, ROUNDS);
        }
        if mode == Mode::Values {
            runtime.check_values()?;
            println!("NXRS_GATT_CLIENT phase=disconnected barrier=recovered");
        } else {
            runtime.disconnect()?;
            println!("NXRS_GATT_CLIENT phase=disconnected barrier=terminal");
        }
        Ok(completed)
    }

    fn cleanup_gatt<S: Ble, D: Driver<CAPACITY>>(
        runtime: &mut Runtime<'_, S, D>,
    ) -> Result<(), &'static str> {
        match runtime.gatt.state() {
            GattState::Recovering => {
                runtime.overall_deadline = runtime.now().saturating_add(RECOVERY_MS);
                runtime.wait_for_recovery()
            }
            GattState::Failed => Err("gatt_recovery_failed"),
            _ if runtime.gatt.is_busy() => {
                runtime
                    .gatt
                    .cancel(runtime.now())
                    .map_err(|_| "gatt_cancel_failed")?;
                runtime.overall_deadline = runtime.now().saturating_add(RECOVERY_MS);
                runtime.wait_for_recovery()
            }
            GattState::Connected(_) => {
                runtime.overall_deadline = runtime.now().saturating_add(DISCONNECT_MS);
                runtime.disconnect()
            }
            _ => Ok(()),
        }
    }

    fn cleanup_scan<S: Ble, D: Driver<CAPACITY>>(
        runtime: &mut Runtime<'_, S, D>,
    ) -> Result<(), &'static str> {
        let stop_error = runtime.scan.request_stop().err();
        let deadline = runtime.now().saturating_add(CLEANUP_MS);
        while runtime.scan.status() != ProviderStatus::Stopped {
            let _ = runtime.pump();
            if !super::scan_cleanup_should_wait(runtime.scan.status(), runtime.now(), deadline)? {
                break;
            }
            runtime.wait_until(deadline);
        }
        runtime.scan.finish().map_err(|_| "scan_finish")?;
        if stop_error.is_some() {
            return Err("scan_shutdown");
        }
        Ok(())
    }

    fn flag(ok: bool) -> &'static str {
        if ok {
            "confirmed"
        } else {
            "failed"
        }
    }

    fn wait_for_native_release(
        released: &Arc<AtomicBool>,
        deadline: Instant,
    ) -> Result<(), &'static str> {
        while !released.load(Ordering::Acquire) {
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return Err("native_close_timeout");
            }
            park(left);
        }
        Ok(())
    }

    fn park(duration: Duration) {
        thread::park_timeout(duration);
    }
}

#[cfg(test)]
mod tests {
    use super::{
        next_pair_step, payload, scan_cleanup_should_wait, Mode, PairStep, CHARACTERISTIC, ROUNDS,
        SERVICE, VALUE_LEN,
    };
    use ble::{
        gatt::{
            Client, Command, Driver, Event, EventKind, Failure, Peer, Reply,
            Request as GattRequest, State, Update,
        },
        Error, PeerId, ProviderStatus, Rejected, RequestId, SessionId,
    };
    use std::{cell::RefCell, collections::VecDeque, rc::Rc};

    const N: usize = VALUE_LEN;
    const SESSION: u64 = 88;

    #[test]
    fn operation_errors_remain_specific_without_native_error_text() {
        for (error, stage) in [
            (Error::Unsupported, "operation_unsupported"),
            (Error::Busy, "operation_busy"),
            (Error::Unavailable, "operation_unavailable"),
            (Error::PermissionDenied, "operation_denied"),
            (Error::InvalidValue, "operation_invalid"),
            (Error::Timeout, "operation_timeout"),
            (Error::Io, "operation_io"),
        ] {
            assert_eq!(super::device::gatt_failure(Failure::Device(error)), stage);
        }
    }

    #[test]
    fn admission_errors_distinguish_pending_work_from_backend_contention() {
        use ble::ControllerError;
        let busy = ControllerError::Device(Error::Busy);
        assert_eq!(super::admission_failure(busy, true), "gatt_pending");
        assert_eq!(super::admission_failure(busy, false), "gatt_backend_busy");
        assert_eq!(
            super::admission_failure(ControllerError::Device(Error::InvalidValue), false),
            "gatt_invalid"
        );
        assert_eq!(
            super::admission_failure(ControllerError::Device(Error::Unavailable), false),
            "gatt_unavailable"
        );
    }

    #[test]
    fn fixture_identifiers_match_the_native_peer() {
        assert_eq!(&super::LEGACY_SERVICE.0[..15], &CHARACTERISTIC.0[..15]);
        assert_ne!(SERVICE, super::LEGACY_SERVICE);
        assert_eq!(SERVICE.0[5], 2);
        assert_eq!(SERVICE.0[15], 1);
        assert_eq!(CHARACTERISTIC.0[15], 2);
    }

    #[derive(Default)]
    struct Shared {
        events: VecDeque<Event<N>>,
        busy: usize,
        admitted: Vec<GattRequest<N>>,
    }
    struct Fake(Rc<RefCell<Shared>>);
    impl Driver<N> for Fake {
        fn session(&self) -> SessionId {
            SessionId::new(SESSION).unwrap()
        }
        fn submit(
            &mut self,
            request: GattRequest<N>,
        ) -> Result<(), Rejected<GattRequest<N>, Error>> {
            let mut state = self.0.borrow_mut();
            if state.busy != 0 {
                state.busy -= 1;
                return Err(Rejected {
                    value: request,
                    error: Error::Busy,
                });
            }
            state.admitted.push(request);
            Ok(())
        }
        fn recover(&mut self, _: RequestId) {}
        fn poll(&mut self) -> Option<Event<N>> {
            self.0.borrow_mut().events.pop_front()
        }
    }

    fn completed(id: RequestId, result: Result<Reply<N>, Failure>) -> Event<N> {
        Event {
            session: SessionId::new(SESSION).unwrap(),
            kind: EventKind::Completed {
                request: id,
                result,
            },
        }
    }

    fn disconnect_after_link_loss() -> (Client<Fake, N>, Rc<RefCell<Shared>>, RequestId) {
        let shared = Rc::new(RefCell::new(Shared::default()));
        let mut client = Client::new(Fake(shared.clone()), 10).unwrap();
        let connection = client
            .submit(
                Command::Connect {
                    peer: Peer {
                        session: SessionId::new(SESSION).unwrap(),
                        id: PeerId(2),
                    },
                },
                0,
                10,
            )
            .unwrap();
        shared
            .borrow_mut()
            .events
            .push_back(completed(connection, Ok(Reply::Connected)));
        assert!(matches!(client.poll(1).unwrap(), Update::Finished(_)));
        let id = client.submit(Command::Disconnect, 2, 10).unwrap();
        shared.borrow_mut().events.push_back(Event {
            session: SessionId::new(SESSION).unwrap(),
            kind: EventKind::Disconnected {
                connection,
                reason: None,
            },
        });
        assert!(matches!(
            client.poll(3).unwrap(),
            Update::Disconnected { .. }
        ));
        assert_eq!(client.state(), State::Disconnecting);
        assert!(!super::unexpected_disconnect(client.state()));
        assert!(client.is_busy());
        assert_eq!(client.poll(4).unwrap(), Update::None);
        (client, shared, id)
    }

    #[test]
    fn expected_link_loss_waits_for_the_terminal_disconnect_barrier() {
        let (mut client, shared, id) = disconnect_after_link_loss();
        shared
            .borrow_mut()
            .events
            .push_back(completed(id, Ok(Reply::Disconnected)));
        assert!(matches!(client.poll(5).unwrap(), Update::Finished(outcome)
            if outcome.id == id && outcome.result == Ok(ble::gatt::Completion::Disconnected)));
        assert_eq!(client.state(), State::Idle);
        assert!(!client.is_busy());
    }

    #[test]
    fn link_loss_without_a_terminal_barrier_still_times_out() {
        let (mut client, _, id) = disconnect_after_link_loss();
        assert!(matches!(client.poll(12).unwrap(), Update::Finished(outcome)
            if outcome.id == id && outcome.result == Err(Failure::Device(Error::Timeout))));
        assert_eq!(client.state(), State::Recovering);
        assert!(super::unexpected_disconnect(client.state()));
    }

    #[test]
    fn backend_busy_returns_command_and_does_not_restart_the_deadline() {
        let shared = Rc::new(RefCell::new(Shared {
            busy: 1,
            ..Shared::default()
        }));
        let mut client = Client::new(Fake(shared.clone()), 10).unwrap();
        let command = Command::Connect {
            peer: Peer {
                session: SessionId::new(SESSION).unwrap(),
                id: PeerId(2),
            },
        };
        let returned = match super::attempt_admission(&mut client, command, 100, 120).unwrap() {
            super::Admission::Retry(returned) => returned,
            _ => panic!("rejected command was not returned"),
        };
        assert_eq!(returned, command);
        assert!(!client.is_busy());
        assert!(shared.borrow().admitted.is_empty());
        assert!(matches!(
            super::attempt_admission(&mut client, returned, 107, 120),
            Ok(super::Admission::Accepted(_))
        ));
        assert_eq!(client.next_deadline_ms(), Some(120));
        assert_eq!(shared.borrow().admitted.len(), 1);
    }

    #[test]
    fn expired_admission_and_existing_pending_work_are_not_retried() {
        let shared = Rc::new(RefCell::new(Shared::default()));
        let mut client = Client::new(Fake(shared.clone()), 10).unwrap();
        let command = Command::Connect {
            peer: Peer {
                session: SessionId::new(SESSION).unwrap(),
                id: PeerId(2),
            },
        };
        assert!(matches!(
            super::attempt_admission(&mut client, command, 120, 120),
            Err("gatt_admission_timeout")
        ));
        assert!(shared.borrow().admitted.is_empty());
        assert!(matches!(
            super::attempt_admission(&mut client, command, 120, 140),
            Ok(super::Admission::Accepted(_))
        ));
        assert!(matches!(
            super::attempt_admission(&mut client, command, 121, 140),
            Err("gatt_pending")
        ));
        assert_eq!(shared.borrow().admitted.len(), 1);
    }

    #[test]
    fn fixture_payload_is_exact_and_pair_progression_has_100_rounds() {
        assert_eq!(
            super::SERVICE.0,
            [
                0x4e, 0x58, 0x52, 0x53, 0x00, 0x02, 0x40, 0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00,
                0x00, 0x01,
            ]
        );
        assert_eq!(
            super::CHARACTERISTIC.0,
            [
                0x4e, 0x58, 0x52, 0x53, 0x00, 0x01, 0x40, 0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00,
                0x00, 0x02,
            ]
        );
        assert_eq!(
            payload(0),
            [0, 0, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
        );
        assert_eq!(&payload(99)[..4], &[0, 0, 0, 99]);
        let mut step = PairStep::Write(0);
        for sequence in 0..ROUNDS {
            assert_eq!(step, PairStep::Write(sequence));
            step = next_pair_step(step, ROUNDS);
            assert_eq!(step, PairStep::Read(sequence));
            step = next_pair_step(step, ROUNDS);
        }
        assert_eq!(step, PairStep::Done);
    }

    #[test]
    fn cancelled_operation_blocks_admission_until_matching_quiescent_barrier() {
        let shared = Rc::new(RefCell::new(Shared::default()));
        let mut client = Client::new(Fake(shared.clone()), 10).unwrap();
        let peer = Peer {
            session: SessionId::new(SESSION).unwrap(),
            id: PeerId(2),
        };
        let id = client.submit(Command::Connect { peer }, 0, 10).unwrap();
        shared
            .borrow_mut()
            .events
            .push_back(completed(id, Ok(Reply::Connected)));
        assert!(matches!(client.poll(1).unwrap(), Update::Finished(_)));
        let id = client
            .submit(
                Command::Discover {
                    service: ble::Uuid([1; 16]),
                    characteristic: ble::Uuid([2; 16]),
                },
                2,
                10,
            )
            .unwrap();
        assert!(
            matches!(client.cancel(3).unwrap(), Update::Finished(outcome)
            if outcome.id == id && outcome.result == Err(Failure::Cancelled))
        );
        assert_eq!(client.state(), State::Recovering);
        assert!(client.submit(Command::Connect { peer }, 4, 10).is_err());
        shared.borrow_mut().events.push_back(Event {
            session: SessionId::new(SESSION).unwrap(),
            kind: EventKind::Quiescent { recovery: id },
        });
        assert_eq!(client.poll(5).unwrap(), Update::Recovered);
        assert_eq!(client.state(), State::Idle);
        assert!(client.submit(Command::Connect { peer }, 6, 10).is_ok());
    }

    #[test]
    fn timed_out_operation_keeps_admission_closed_until_recovery_barrier() {
        let shared = Rc::new(RefCell::new(Shared::default()));
        let mut client = Client::new(Fake(shared.clone()), 10).unwrap();
        let peer = Peer {
            session: SessionId::new(SESSION).unwrap(),
            id: PeerId(3),
        };
        let id = client.submit(Command::Connect { peer }, 0, 5).unwrap();
        assert!(matches!(client.poll(5).unwrap(), Update::Finished(outcome)
            if outcome.id == id && outcome.result == Err(Failure::Device(Error::Timeout))));
        assert_eq!(client.state(), State::Recovering);
        assert!(client.submit(Command::Connect { peer }, 6, 5).is_err());
        shared.borrow_mut().events.push_back(Event {
            session: SessionId::new(SESSION).unwrap(),
            kind: EventKind::Quiescent { recovery: id },
        });
        assert_eq!(client.poll(7).unwrap(), Update::Recovered);
        assert!(client.submit(Command::Connect { peer }, 8, 5).is_ok());
    }

    #[test]
    fn mode_defaults_to_full_and_cancel_is_explicit() {
        assert_eq!(Mode::parse(None), Some(Mode::Full));
        assert_eq!(Mode::parse(Some("cancel")), Some(Mode::Cancel));
        assert_eq!(Mode::parse(Some("values")), Some(Mode::Values));
        assert_eq!(Mode::parse(Some("legacy")), Some(Mode::Legacy));
        assert_eq!(Mode::Legacy.service(), super::LEGACY_SERVICE);
        for mode in [Mode::Full, Mode::Cancel, Mode::Values] {
            assert_eq!(mode.service(), SERVICE);
        }
        assert_eq!(Mode::parse(Some("other")), None);
    }

    #[test]
    fn both_entries_reject_unknown_modes_and_extra_arguments() {
        for (args, expected) in [
            (vec![], Some(Mode::Full)),
            (vec!["cancel"], Some(Mode::Cancel)),
            (vec!["values"], Some(Mode::Values)),
            (vec!["legacy"], Some(Mode::Legacy)),
            (vec!["full"], None),
            (vec!["other"], None),
            (vec!["values", "extra"], None),
            (vec!["cancel", "values"], None),
        ] {
            assert_eq!(
                Mode::from_args(args.into_iter().map(String::from)),
                expected
            );
        }
    }

    #[test]
    fn scan_cleanup_does_not_wait_after_pump_reaches_stopped() {
        let deadline = 5_000;
        assert_eq!(
            scan_cleanup_should_wait(ProviderStatus::Stopped, 0, deadline),
            Ok(false)
        );
        assert_eq!(
            scan_cleanup_should_wait(ProviderStatus::Stopping, 0, deadline),
            Ok(true)
        );
        assert_eq!(
            scan_cleanup_should_wait(ProviderStatus::Stopping, deadline, deadline),
            Err("scan_shutdown_timeout")
        );
    }

    #[test]
    fn validation_values_reject_prefixes_and_corruption() {
        for length in [44, super::CAPACITY] {
            let mut bytes: Vec<u8> = (0..length).map(|index| (length + index) as u8).collect();
            assert!(super::validation_value_matches(&bytes, length));
            assert!(!super::validation_value_matches(
                &bytes[..length - 1],
                length
            ));
            bytes[length - 1] ^= 1;
            assert!(!super::validation_value_matches(&bytes, length));
        }
    }
}
