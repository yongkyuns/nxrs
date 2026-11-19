//! Fixed product lifecycle; qualification controls stay in the stream fixture.
#![forbid(unsafe_code)]
use crate::commissioning;
use camera::{Camera, JpegQuality};
use connectivity_service::Reconnector;
use home_cam::{update::Update, LiveCamera, StreamOutput, TlsOptions};

use std::{
    net::TcpListener,
    sync::mpsc::{sync_channel, Receiver, SyncSender, TrySendError},
    time::{Duration, Instant},
};
use wifi::{Command, DeliveryError, Event, LinkState, ProviderStatus, Rejected};

const BYTES: usize = 320 * 240 * 2;
const HEALTH_AGE_MS: u64 = 5000;
const DELIVERY_TIMEOUT: Duration = Duration::from_secs(30);
const RECOVERY_RESET_FRAMES: u64 = 32;
const MAX_RETRY_DELAY: u64 = 30;

struct Sink {
    events: SyncSender<Event>,
    owner: std::thread::Thread,
}
impl Sink {
    fn try_emit(&mut self, event: Event) -> Result<(), Rejected<Event, DeliveryError>> {
        let result = self.events.try_send(event).map_err(|e| match e {
            TrySendError::Full(value) => Rejected {
                value,
                error: DeliveryError::Full,
            },
            TrySendError::Disconnected(value) => Rejected {
                value,
                error: DeliveryError::Closed,
            },
        });
        self.owner.unpark();
        result
    }
}
fn now(origin: Instant) -> u64 {
    origin.elapsed().as_millis() as u64
}
fn network_poll<W: wifi::Wifi, F: FnMut() -> Result<Command, wifi::Error>>(
    network: &mut Reconnector<W, F>,
    events: &Receiver<Event>,
    elapsed: u64,
) -> Result<(), String> {
    for _ in 0..16 {
        match events.try_recv() {
            Ok(event) => network
                .on_event(event, elapsed)
                .map_err(|e| format!("Wi-Fi event {e:?}"))?,
            Err(std::sync::mpsc::TryRecvError::Empty) => break,
            Err(_) => return Err("Wi-Fi event channel closed".into()),
        }
    }
    network
        .poll(elapsed)
        .map_err(|e| format!("Wi-Fi poll {e:?}"))
}
fn wait_addressed<W: wifi::Wifi, F: FnMut() -> Result<Command, wifi::Error>>(
    network: &mut Reconnector<W, F>,
    events: &Receiver<Event>,
    origin: Instant,
) -> Result<(), String> {
    let until = Instant::now() + Duration::from_secs(90);
    while Instant::now() < until {
        network_poll(network, events, now(origin))?;
        if network.link() == LinkState::Addressed {
            return Ok(());
        }
        std::thread::park_timeout(Duration::from_millis(5));
    }
    Err("Wi-Fi did not reach addressed state".into())
}
fn retire_network<W: wifi::Wifi, F: FnMut() -> Result<Command, wifi::Error>>(
    network: &mut Reconnector<W, F>,
    events: &Receiver<Event>,
    origin: Instant,
) -> Result<(), String> {
    let stop = network
        .request_disconnect_and_stop(now(origin))
        .map_err(|e| format!("Wi-Fi stop {e:?}"));
    let until = Instant::now() + Duration::from_secs(35);
    while network.status() != ProviderStatus::Stopped && Instant::now() < until {
        let _ = network_poll(network, events, now(origin));
        std::thread::park_timeout(Duration::from_millis(5));
    }
    let joined = network.finish().map_err(|e| format!("Wi-Fi join {e:?}"));
    stop.and(joined)
}

enum Outcome {
    SaveWifi,
    Renew,
    Retry,
    Update,
    Healthy(home_cam::startup::Health),
}

fn camera_failure(stage: &str, error: camera::DeviceError) -> Result<Outcome, String> {
    if matches!(
        error,
        camera::DeviceError::Io | camera::DeviceError::Timeout
    ) {
        eprintln!("CAMERA_RETRY stage={stage} error={error:?}");
        Ok(Outcome::Retry)
    } else {
        Err(format!("camera {stage} {error:?}"))
    }
}

fn run_live<D: Camera, O: StreamOutput>(
    mut live: LiveCamera<D, BYTES, Vec<u8>, O>,
    mut poll_network: impl FnMut(u64) -> Result<(bool, Option<u64>), String>,
    origin: Instant,
    update: Option<&Update>,
    trial: bool,
    budget: Option<detector::ExecutionBudget>,
    retry_delay: &mut u64,
) -> Result<Outcome, String> {
    let mut restart = Outcome::Renew;
    let operation: Result<bool, String> = (|| {
        if let Some(budget) = budget {
            let worker = detection_service::Worker::spawn(
                detection_service::WorkerOptions {
                    frame_capacity: BYTES,
                    sample_interval_ms: budget.sample_interval_ms,
                    stack_bytes: budget.stack_bytes,
                },
                || {
                    // Keep synchronous inference below capture and video delivery.
                    execution::configure_current(execution::Priority::Background)
                        .map_err(|_| detection_service::Error::Worker)?;
                    detector::open(detector::Config::default()).map_err(Into::into)
                },
            )
            .map_err(|e| format!("detector startup {e:?}"))?;
            live.attach_detection(
                home_cam::SecurityDetection::new(worker, detection_service::Policy::default())
                    .map_err(|e| format!("detection policy {e:?}"))?,
            )
            .map_err(|e| format!("detection attachment {e:?}"))?;
        }
        match live.start(320, 240) {
            Ok(_) => {}
            Err(home_cam::Error::Camera(camera_service::Error::Device(error))) => {
                restart = camera_failure("start", error)?;
                return Ok(false);
            }
            Err(error) => return Err(format!("capture start {error:?}")),
        }
        let (mut delivered, mut captured) = (0, 0);
        let (mut last_delivery, mut last_capture) = (Instant::now(), Instant::now());
        loop {
            if update.is_some_and(home_cam::update::Update::ready) {
                return Ok(false);
            }
            if last_delivery.elapsed() >= DELIVERY_TIMEOUT
                && update.is_none_or(home_cam::update::Update::retire_if_idle)
            {
                return Ok(false);
            }
            let elapsed = now(origin);
            let (online, network_deadline) = poll_network(elapsed)?;
            match live.poll(elapsed, online) {
                Ok(_) => {}
                Err(home_cam::Error::Camera(camera_service::Error::Device(error))) => {
                    restart = camera_failure("read", error)?;
                    return Ok(false);
                }
                Err(error) => return Err(format!("stream {error:?}")),
            }
            if trial && online && home_cam::startup::healthy(&live, elapsed, HEALTH_AGE_MS) {
                // Consuming retirement below joins before handing health to confirmation.
                return Ok(true);
            }
            let stats = live.stats();
            if stats.stream.frames_completed > delivered {
                delivered = stats.stream.frames_completed;
                last_delivery = Instant::now();
            }
            if stats.captured > captured {
                captured = stats.captured;
                last_capture = Instant::now();
                if captured == RECOVERY_RESET_FRAMES {
                    *retry_delay = 0;
                }
            }
            if last_capture.elapsed() > Duration::from_secs(5) {
                restart = camera_failure("capture", camera::DeviceError::Timeout)?;
                return Ok(false);
            }
            let wake = network_deadline
                .into_iter()
                .chain(live.next_poll_at_ms())
                .min()
                .unwrap_or(elapsed + 5)
                .min(elapsed + 5);
            std::thread::park_timeout(Duration::from_millis(
                wake.saturating_sub(now(origin)).max(1),
            ));
        }
    })();
    if matches!(operation, Ok(true)) {
        return home_cam::startup::retire_healthy(live, now(origin), HEALTH_AGE_MS)
            .map(Outcome::Healthy)
            .map_err(|e| format!("startup retirement {e:?}"));
    }
    let stopped = live.stop().map_err(|e| format!("camera retirement {e:?}"));
    stopped?;
    operation?;
    Ok(if update.is_some_and(home_cam::update::Update::ready) {
        Outcome::Update
    } else {
        restart
    })
}

fn run_camera_sessions(
    update: &mut Option<Update>,
    mut run_session: impl FnMut(Option<&Update>, &mut u64, Duration) -> Result<Outcome, String>,
) -> Result<Outcome, String> {
    let mut retry_delay = 0;
    let mut pause = Duration::ZERO;
    loop {
        match run_session(update.as_ref(), &mut retry_delay, pause)? {
            Outcome::Retry => {
                pause = Duration::from_secs(retry_delay);
                retry_delay = (retry_delay * 2).clamp(1, MAX_RETRY_DELAY);
            }
            Outcome::Renew => pause = Duration::ZERO,
            outcome => return Ok(outcome),
        }
        *update = update
            .take()
            .map(Update::renew)
            .transpose()
            .map_err(|e| format!("update renewal {e:?}"))?;
    }
}

pub(crate) fn run() -> Result<(), String> {
    // Adoption precedes storage, first-time input and network initialization.
    let watchdog = watchdog::adopt().map_err(|e| format!("watchdog adopt {e:?}"))?;
    let boot = boot::open().map_err(|e| format!("boot open {e:?}"))?;
    let mut trial =
        home_cam::startup::Trial::begin(boot, watchdog).map_err(|e| format!("startup {e:?}"))?;
    let budget = detector::execution_budget();
    let mut store = Some(config::open().map_err(|e| format!("config open {e:?}"))?);
    let mut settings = commissioning::load(store.as_mut().ok_or("missing store")?, console::open)?;
    if !settings.save_wifi {
        drop(store.take());
    }
    let origin = Instant::now();
    let mut listener = None;
    loop {
        let mut update = if trial.is_none() {
            Some(
                home_cam::update::Update::new(update_capacity()?, Duration::from_secs(120))
                    .map_err(|e| format!("update setup {e:?}"))?,
            )
        } else {
            None
        };
        let (sender, events) = sync_channel(8);
        let controller = wifi::open({
            let mut sink = Sink {
                events: sender,
                owner: std::thread::current(),
            };
            move |event| sink.try_emit(event)
        })
        .map_err(|e| format!("Wi-Fi open {e:?}"))?;
        let mut network = Reconnector::new(controller, || settings.wifi.command());
        let mut maintenance = None;
        let operation: Result<Outcome, String> = (|| {
            wait_addressed(&mut network, &events, origin)?;
            if settings.save_wifi {
                return Ok(Outcome::SaveWifi);
            }
            if trial.is_none() {
                match ssh::start() {
                    Ok(session) => maintenance = session,
                    Err(error) => eprintln!("SSH unavailable: {error}"),
                }
            }
            if listener.is_none() {
                listener =
                    Some(TcpListener::bind("0.0.0.0:8443").map_err(|e| format!("listener {e:?}"))?);
            }
            run_camera_sessions(&mut update, |update, retry_delay, pause| {
                let until = Instant::now() + pause;
                loop {
                    network_poll(&mut network, &events, now(origin))?;
                    let remaining = until.saturating_duration_since(Instant::now());
                    if remaining.is_zero() && network.link() == LinkState::Addressed {
                        break;
                    }
                    std::thread::park_timeout(if remaining.is_zero() {
                        Duration::from_millis(50)
                    } else {
                        remaining.min(Duration::from_millis(50))
                    });
                }
                let mut camera = match camera::open() {
                    Ok(camera) => camera,
                    Err(error) => return camera_failure("open", error),
                };
                let quality = JpegQuality::new(81).map_err(|e| format!("quality {e:?}"))?;
                if let Err(error) = camera.set_jpeg_quality(quality) {
                    return camera_failure("quality", error);
                }
                let commission = settings.commission.clone();
                let composed = LiveCamera::<_, BYTES, _>::new_tls(
                    camera,
                    listener
                        .as_ref()
                        .ok_or("missing listener")?
                        .try_clone()
                        .map_err(|e| format!("listener clone {e:?}"))?,
                    std::array::from_fn(|_| vec![0; BYTES]),
                    stream_service::Options::default(),
                    TlsOptions {
                        stack_bytes: 64 * 1024,
                        poll_interval: Duration::from_millis(2),
                    },
                    move || {
                        execution::configure_current(execution::Priority::Normal)
                            .map_err(|_| home_cam::Error::Worker)?;
                        home_cam::bootstrap::initialize(commission)
                    },
                );
                let mut live = match composed {
                    Ok(live) => live,
                    Err(home_cam::Error::Tls(
                        error @ (tls::Error::Io | tls::Error::Time | tls::Error::Authentication),
                    )) => {
                        eprintln!("CAMERA_RETRY stage=tls error={error:?}");
                        return Ok(Outcome::Retry);
                    }
                    Err(error) => return Err(format!("TLS composition {error:?}")),
                };
                if let Some(update) = update {
                    live.enable_updates(update.clone())
                        .map_err(|e| format!("update receiver {e:?}"))?;
                }
                if audio::AVAILABLE {
                    live.enable_microphone(origin)
                        .map_err(|e| format!("microphone authorization {e:?}"))?;
                }
                run_live(
                    live,
                    |elapsed| {
                        network_poll(&mut network, &events, elapsed)?;
                        Ok((
                            network.link() == LinkState::Addressed,
                            network.next_deadline_ms(),
                        ))
                    },
                    origin,
                    update,
                    trial.is_some(),
                    budget,
                    retry_delay,
                )
            })
        })();
        // Reap the maintenance task before releasing Wi-Fi or modifying flash.
        if let Some(session) = maintenance {
            session.stop().map_err(|e| format!("SSH stop {e:?}"))?;
        }
        let retired = retire_network(&mut network, &events, origin);
        let outcome = operation?;
        retired?;
        match outcome {
            Outcome::SaveWifi => {
                use wifi::QuiescenceControl;
                let mut quiet =
                    wifi::open_for_quiescence().map_err(|e| format!("config maintenance {e:?}"))?;
                quiet.quiesce().map_err(|e| format!("config quiet {e:?}"))?;
                let saved = settings
                    .wifi
                    .save(store.as_mut().ok_or("missing profile store")?)
                    .map_err(|e| format!("config save {e:?}"));
                let resumed = quiet.resume().map_err(|e| format!("config resume {e:?}"));
                saved?;
                resumed?;
                drop(quiet);
                drop(store.take());
                settings.save_wifi = false;
            }
            Outcome::Healthy(health) => {
                use boot::Control;
                let startup = trial.take().ok_or("missing trial owner")?;
                let mut quiet = wifi::open_for_quiescence()
                    .map_err(|e| format!("confirmation maintenance {e:?}"))?;
                startup
                    .complete(health, &mut quiet, || now(origin))
                    .map_err(|e| format!("trial confirmation {e:?}"))?;
                drop(quiet);
                let mut boot = boot::open().map_err(|e| format!("confirmed boot {e:?}"))?;
                match boot
                    .reboot_confirmed()
                    .map_err(|e| format!("confirmed reboot {e:?}"))? {}
            }
            Outcome::Update => {
                use boot::Control;
                let mut boot = boot::open().map_err(|e| format!("update boot {e:?}"))?;
                let mut quiet =
                    wifi::open_for_quiescence().map_err(|e| format!("update maintenance {e:?}"))?;
                update
                    .take()
                    .ok_or("missing update")?
                    .stage(&mut boot, &mut quiet)
                    .map_err(|e| format!("update stage {e:?}"))?;
                drop(quiet);
                match boot.reboot().map_err(|e| format!("update reboot {e:?}"))? {}
            }
            Outcome::Renew | Outcome::Retry => return Err("unretired renewal".into()),
        }
    }
}

fn update_capacity() -> Result<u32, String> {
    use boot::Control;
    let mut boot = boot::open().map_err(|e| format!("update capacity open {e:?}"))?;
    // Bound reception RAM independently of the selected storage geometry.
    boot.update_capacity()
        .map(|bytes| bytes.min(2 * 1024 * 1024))
        .map_err(|e| format!("update capacity {e:?}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use camera::{Capture, DeviceError, Format};
    use std::cell::Cell;

    struct FailingRead<'a> {
        retired: &'a Cell<u32>,
        stop: Result<(), DeviceError>,
        frames: u64,
        format: Option<Format>,
    }

    impl Camera for FailingRead<'_> {
        fn start(&mut self, format: Format) -> Result<Format, DeviceError> {
            self.format = Some(format);
            Ok(format)
        }

        fn poll_frame(
            &mut self,
            now: u64,
            bytes: &mut [u8],
        ) -> Result<Option<Capture>, DeviceError> {
            self.frames = self.frames.checked_sub(1).ok_or(DeviceError::Io)?;
            bytes[..4].copy_from_slice(&[0xff, 0xd8, 0xff, 0xd9]);
            Ok(Some(Capture {
                format: self.format.unwrap(),
                len: 4,
                timestamp_ms: now,
            }))
        }

        fn stop(&mut self) -> Result<(), DeviceError> {
            self.stop?;
            self.retired.set(self.retired.get() + 1);
            Ok(())
        }
    }

    #[test]
    fn repeated_read_errors_back_off_until_capture_recovers() {
        for stop in [Ok(()), Err(DeviceError::Io)] {
            let retired = Cell::new(0);
            let mut pauses = Vec::new();
            let mut update = Some(Update::new(1024, Duration::from_secs(1)).unwrap());
            let result = run_camera_sessions(&mut update, |update, retry_delay, pause| {
                assert_eq!(retired.get() as usize, pauses.len());
                pauses.push(pause.as_secs());
                if pauses.len() == 10 {
                    return Ok(Outcome::Update);
                }
                let live = LiveCamera::new(
                    FailingRead {
                        retired: &retired,
                        stop,
                        frames: if pauses.len() == 8 {
                            RECOVERY_RESET_FRAMES
                        } else {
                            0
                        },
                        format: None,
                    },
                    TcpListener::bind("127.0.0.1:0").unwrap(),
                    vec![0; BYTES],
                    vec![0; BYTES],
                    vec![0; BYTES],
                    stream_service::Options::default(),
                )
                .unwrap();
                run_live(
                    live,
                    |_| Ok((false, None)),
                    Instant::now(),
                    update,
                    false,
                    None,
                    retry_delay,
                )
            });
            if stop.is_ok() {
                assert!(matches!(result, Ok(Outcome::Update)));
                assert_eq!(pauses, [0, 0, 1, 2, 4, 8, 16, 30, 0, 1]);
            } else {
                assert_eq!(
                    result.err().as_deref(),
                    Some("camera retirement Camera(Device(Io))")
                );
                assert_eq!(pauses, [0]);
            }
        }
    }
}
