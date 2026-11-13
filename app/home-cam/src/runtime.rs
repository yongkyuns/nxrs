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
const RENEW_AFTER: Duration = Duration::from_secs(45 * 60);
const DELIVERY_TIMEOUT: Duration = Duration::from_secs(30);
const RECOVERY_REARM_FRAMES: u64 = 32;

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
    Update,
    Healthy(home_cam::startup::Health),
}

fn run_live<D: Camera, O: StreamOutput>(
    mut live: LiveCamera<D, BYTES, Vec<u8>, O>,
    mut poll_network: impl FnMut(u64) -> Result<(bool, Option<u64>), String>,
    origin: Instant,
    update: Option<&Update>,
    trial: bool,
    budget: Option<detector::ExecutionBudget>,
    can_recover: &mut bool,
) -> Result<Outcome, String> {
    let operation: Result<bool, String> = (|| {
        if let Some(budget) = budget {
            let worker = detection_service::Worker::spawn(
                detection_service::WorkerOptions {
                    frame_capacity: BYTES,
                    sample_interval_ms: budget.sample_interval_ms,
                    stack_bytes: budget.stack_bytes,
                },
                || {
                    execution::configure_current(execution::Priority::Normal)
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
        live.start(320, 240)
            .map_err(|e| format!("capture start {e:?}"))?;
        let started = Instant::now();
        let (mut delivered, mut captured) = (0, 0);
        let (mut last_delivery, mut last_capture) = (Instant::now(), Instant::now());
        loop {
            if update.is_some_and(home_cam::update::Update::ready) {
                return Ok(false);
            }
            if (started.elapsed() >= RENEW_AFTER || last_delivery.elapsed() >= DELIVERY_TIMEOUT)
                && update.is_none_or(home_cam::update::Update::retire_if_idle)
            {
                return Ok(false);
            }
            let elapsed = now(origin);
            let (online, network_deadline) = poll_network(elapsed)?;
            match live.poll(elapsed, online) {
                Ok(_) => {}
                Err(home_cam::Error::Camera(camera_service::Error::Device(
                    error @ (camera::DeviceError::Io | camera::DeviceError::Timeout),
                ))) if *can_recover => {
                    *can_recover = false;
                    eprintln!("CAMERA_READ_RETRY error={error:?}");
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
                if !*can_recover && captured >= RECOVERY_REARM_FRAMES {
                    *can_recover = true;
                }
            }
            if last_capture.elapsed() > Duration::from_secs(5) {
                return Err("camera stalled".into());
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
    operation?;
    stopped?;
    Ok(if update.is_some_and(home_cam::update::Update::ready) {
        Outcome::Update
    } else {
        Outcome::Renew
    })
}

fn run_camera_sessions(
    update: &mut Option<Update>,
    mut run_session: impl FnMut(Option<&Update>, &mut bool) -> Result<Outcome, String>,
) -> Result<Outcome, String> {
    let mut can_recover = true;
    loop {
        match run_session(update.as_ref(), &mut can_recover)? {
            Outcome::Renew => {
                *update = update
                    .take()
                    .map(Update::renew)
                    .transpose()
                    .map_err(|e| format!("update renewal {e:?}"))?;
            }
            outcome => return Ok(outcome),
        }
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
    let mut retry_delay = 1;
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
        let mut retry_network = false;
        let operation: Result<Outcome, String> = (|| {
            wait_addressed(&mut network, &events, origin)?;
            if settings.save_wifi {
                return Ok(Outcome::SaveWifi);
            }
            if listener.is_none() {
                listener =
                    Some(TcpListener::bind("0.0.0.0:8443").map_err(|e| format!("listener {e:?}"))?);
            }
            run_camera_sessions(&mut update, |update, can_recover| {
                let mut camera = camera::open().map_err(|e| format!("camera open {e:?}"))?;
                camera
                    .set_jpeg_quality(JpegQuality::new(81).map_err(|e| format!("quality {e:?}"))?)
                    .map_err(|e| format!("quality setup {e:?}"))?;
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
                        execution::configure_current(execution::Priority::Background)
                            .map_err(|_| home_cam::Error::Worker)?;
                        home_cam::bootstrap::initialize(commission)
                    },
                );
                let mut live = match composed {
                    Ok(live) => live,
                    Err(error) => {
                        retry_network = matches!(
                            error,
                            home_cam::Error::Tls(
                                tls::Error::Io | tls::Error::Time | tls::Error::Authentication
                            )
                        );
                        return Err(format!("TLS composition {error:?}"));
                    }
                };
                if let Some(update) = update {
                    live.enable_updates(update.clone())
                        .map_err(|e| format!("update receiver {e:?}"))?;
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
                    can_recover,
                )
            })
        })();
        let retired = retire_network(&mut network, &events, origin);
        if retry_network && retired.is_ok() {
            std::thread::sleep(Duration::from_secs(retry_delay));
            retry_delay = (retry_delay * 2).min(30);
            continue;
        }
        let outcome = operation?;
        retired?;
        retry_delay = 1;
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
            Outcome::Renew => return Err("unretired renewal".into()),
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
    fn read_error_recovery_requires_clean_retirement_and_rearms_after_capture() {
        for (stop, recovery_frames, expected_sessions, expected_error) in [
            (Ok(()), 0, 2, "stream Camera(Device(Io))"),
            (
                Ok(()),
                RECOVERY_REARM_FRAMES,
                3,
                "stream Camera(Device(Io))",
            ),
            (
                Err(DeviceError::Io),
                0,
                1,
                "camera retirement Camera(Device(Io))",
            ),
        ] {
            let retired = Cell::new(0);
            let mut sessions = 0;
            let mut update = Some(Update::new(1024, Duration::from_secs(1)).unwrap());
            let result = run_camera_sessions(&mut update, |update, can_recover| {
                assert_eq!(retired.get(), sessions);
                sessions += 1;
                let live = LiveCamera::new(
                    FailingRead {
                        retired: &retired,
                        stop,
                        frames: if sessions == 2 { recovery_frames } else { 0 },
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
                    can_recover,
                )
            });
            assert_eq!(result.err().as_deref(), Some(expected_error));
            assert_eq!(sessions, expected_sessions);
        }
    }
}
