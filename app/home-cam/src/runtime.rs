//! Fixed product lifecycle; qualification controls stay in the stream fixture.
#![forbid(unsafe_code)]
use crate::commissioning;
use camera::{Camera, JpegQuality};
use connectivity_service::Reconnector;
use home_cam::{LiveCamera, TlsOptions, TlsOutput};
use home_cam_platform::{self as platform, Resources, Worker};
use std::{
    net::TcpListener,
    sync::mpsc::{sync_channel, Receiver, SyncSender, TrySendError},
    time::{Duration, Instant},
};
use wifi::{Command, DeliveryError, Event, EventSink, LinkState, ProviderStatus, Rejected};

const BYTES: usize = 320 * 240 * 2;
const HEALTH_AGE_MS: u64 = 5000;
const RENEW_AFTER: Duration = Duration::from_secs(45 * 60);
const DELIVERY_TIMEOUT: Duration = Duration::from_secs(30);

struct Sink {
    events: SyncSender<Event>,
    owner: std::thread::Thread,
}
impl EventSink<Event> for Sink {
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

fn run_live<D: Camera, W: wifi::Wifi, F: FnMut() -> Result<Command, wifi::Error>>(
    mut live: LiveCamera<D, BYTES, Vec<u8>, TlsOutput<Vec<u8>>>,
    network: &mut Reconnector<W, F>,
    events: &Receiver<Event>,
    origin: Instant,
    update: Option<&home_cam::update::Update>,
    trial: bool,
    resources: Resources,
) -> Result<Outcome, String> {
    let operation: Result<bool, String> = (|| {
        if let Some(budget) = resources.detector {
            let worker = detection_service::Worker::spawn(
                detection_service::WorkerOptions {
                    frame_capacity: BYTES,
                    sample_interval_ms: budget.sample_ms,
                    stack_bytes: budget.stack_bytes,
                },
                || {
                    platform::initialize_worker(Worker::Detector)
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
            network_poll(network, events, elapsed)?;
            let online = network.link() == LinkState::Addressed;
            live.poll(elapsed, online)
                .map_err(|e| format!("stream {e:?}"))?;
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
            }
            if last_capture.elapsed() > Duration::from_secs(5) {
                return Err("camera stalled".into());
            }
            let wake = network
                .next_deadline_ms()
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

pub(crate) fn run() -> Result<(), String> {
    // Adoption precedes storage, first-time input and network initialization.
    let watchdog = watchdog::adopt().map_err(|e| format!("watchdog adopt {e:?}"))?;
    let boot = boot::open().map_err(|e| format!("boot open {e:?}"))?;
    let mut trial =
        home_cam::startup::Trial::begin(boot, watchdog).map_err(|e| format!("startup {e:?}"))?;
    let resources = platform::resources();
    let mut store = Some(config::open().map_err(|e| format!("config open {e:?}"))?);
    let mut settings = commissioning::load(
        store.as_mut().ok_or("missing store")?,
        platform::Console::open,
    )?;
    if !settings.save_wifi {
        drop(store.take());
    }
    platform::initialize_network().map_err(|e| format!("network initialization {e:?}"))?;
    let origin = Instant::now();
    let mut listener = None;
    let mut retry_delay = 1;
    loop {
        let mut update = if trial.is_none() {
            Some(
                home_cam::update::Update::new(resources.update_capacity, Duration::from_secs(120))
                    .map_err(|e| format!("update setup {e:?}"))?,
            )
        } else {
            None
        };
        let (sender, events) = sync_channel(8);
        let controller = wifi::open(Sink {
            events: sender,
            owner: std::thread::current(),
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
            loop {
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
                        stack_bytes: resources.tls_stack_bytes,
                        poll_interval: resources.tls_poll_interval,
                    },
                    move || {
                        platform::initialize_worker(Worker::Tls)
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
                if let Some(update) = &update {
                    live.enable_updates(update.clone())
                        .map_err(|e| format!("update receiver {e:?}"))?;
                }
                match run_live(
                    live,
                    &mut network,
                    &events,
                    origin,
                    update.as_ref(),
                    trial.is_some(),
                    resources,
                )? {
                    Outcome::Renew => {
                        update = update
                            .take()
                            .map(home_cam::update::Update::renew)
                            .transpose()
                            .map_err(|e| format!("update renewal {e:?}"))?;
                    }
                    outcome => return Ok(outcome),
                }
            }
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
                let startup = trial.take().ok_or("missing trial owner")?;
                let mut quiet = wifi::open_for_quiescence()
                    .map_err(|e| format!("confirmation maintenance {e:?}"))?;
                startup
                    .complete(health, &mut quiet, || now(origin))
                    .map_err(|e| format!("trial confirmation {e:?}"))?;
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
