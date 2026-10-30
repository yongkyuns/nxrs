//! Physical qualification composition, not a hardware-specific streaming app.
#[cfg(target_os = "nuttx")]
#[path = "tls_console.rs"]
mod console;
#[cfg(any(target_os = "nuttx", test))]
mod session;
#[cfg(any(target_os = "nuttx", test))]
#[cfg_attr(test, allow(dead_code))]
mod model {
    include!(concat!(env!("OUT_DIR"), "/stream_detector.rs"));
}
#[cfg(target_os = "nuttx")]
mod device {
    use camera::{Camera, JpegQuality};
    use camera_stream::{LiveCamera, StreamOutput, TlsOptions};
    use connectivity_service::Reconnector;
    use std::io::Read;
    use std::net::{Ipv4Addr, TcpListener};
    use std::sync::{
        atomic::{AtomicU8, Ordering},
        mpsc::{sync_channel, Receiver, SyncSender, TrySendError},
        Arc,
    };
    use std::time::{Duration, Instant};
    use wifi::{
        Command, Credentials, DeliveryError, Event, EventSink, LinkState, Passphrase,
        ProviderStatus, Rejected, Ssid,
    };

    const BYTES: usize = 320 * 240 * 2;
    unsafe extern "C" {
        fn nxrs_wifi_test_credentials(
            ssid: *mut u8,
            password: *mut u8,
            ssid_length: *mut u32,
            password_length: *mut u32,
        ) -> i32;
        fn nxrs_stream_metrics(values: *mut u32, times: *mut u64) -> i32;
        fn nxrs_stream_address(address: *mut u8) -> i32;
        fn nxrs_stream_console_ready() -> i32;

        fn nxrs_stream_worker_priority(priority: i32) -> i32;

        fn nxrs_stream_worker_stack(values: *mut u32) -> i32;
    }

    struct Profile {
        ssid: Ssid,
        password: [u8; 63],
        length: usize,
    }
    impl Profile {
        fn read() -> Result<Self, String> {
            let mut ssid = [0; 32];
            let mut password = [0; 63];
            let (mut ssid_length, mut password_length) = (0, 0);
            // SAFETY: fixed-size writable arrays/scalars match the no-echo C ABI.
            let result = unsafe {
                nxrs_wifi_test_credentials(
                    ssid.as_mut_ptr(),
                    password.as_mut_ptr(),
                    &mut ssid_length,
                    &mut password_length,
                )
            };
            if result != 0 || ssid_length > 32 || password_length > 63 {
                return Err("credential input failed".into());
            }
            let ssid = Ssid::new(&ssid[..ssid_length as usize]).map_err(|_| "invalid SSID")?;
            let profile = Self {
                ssid,
                password,
                length: password_length as usize,
            };
            profile.command().map_err(|_| "invalid credentials")?;
            Ok(profile)
        }
        fn command(&self) -> Result<Command, wifi::Error> {
            let credentials = if self.length == 0 {
                Credentials::Open
            } else {
                let text = std::str::from_utf8(&self.password[..self.length])
                    .map_err(|_| wifi::Error::InvalidConfig)?;
                Credentials::Wpa2Personal(Passphrase::new(text)?)
            };
            Ok(Command::Connect {
                ssid: self.ssid,
                credentials,
            })
        }
    }

    struct Sink {
        events: SyncSender<Event>,
        owner: std::thread::Thread,
    }
    impl EventSink<Event> for Sink {
        fn try_emit(&mut self, event: Event) -> Result<(), Rejected<Event, DeliveryError>> {
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
            self.owner.unpark();
            result
        }
    }

    fn now(origin: Instant) -> u64 {
        origin.elapsed().as_millis() as u64
    }

    fn stage(phase: &str) {
        println!("NXRS_STREAM_STAGE phase={phase}");
    }

    fn report_model_stack() {
        let mut values = [0u32; 2];
        // SAFETY: target SDK observes this calling worker; two scalar outputs.
        if unsafe { nxrs_stream_worker_stack(values.as_mut_ptr()) } == 0 {
            println!(
                "NXRS_DETECTION_STACK budget={} used={}",
                values[0], values[1]
            );
        }
    }

    struct QualifiedDetector<T: detector::Detector>(T);

    impl<T: detector::Detector> detector::Detector for QualifiedDetector<T> {
        fn capabilities(&self) -> detector::Capabilities {
            self.0.capabilities()
        }
        fn detect(
            &mut self,
            image: detector::Image<'_>,
        ) -> Result<detector::DetectionResult, detector::DeviceError> {
            // SAFETY: qualification observes the native SDK context on this CPU.
            let previous = super::model::compute_state();
            let result = self.0.detect(image);
            if super::model::compute_state() != previous {
                return Err(detector::DeviceError::Model);
            }
            result
        }
    }

    impl<T: detector::Detector> Drop for QualifiedDetector<T> {
        fn drop(&mut self) {
            report_model_stack();
        }
    }

    fn address() -> Result<Ipv4Addr, String> {
        let mut bytes = [0; 4];
        // SAFETY: writable four-byte IPv4 output, queried with native headers.
        if unsafe { nxrs_stream_address(bytes.as_mut_ptr()) } != 0 {
            return Err("no station IPv4 address".into());
        }
        Ok(Ipv4Addr::from(bytes))
    }

    fn report<D: camera::Camera, O: StreamOutput>(
        live: &LiveCamera<D, BYTES, Vec<u8>, O>,
        elapsed: u64,
        online: bool,
        attempts: u64,
        rejoined: u64,
        previous: &mut Option<[u64; 3]>,
    ) -> Result<(), String> {
        let mut memory = [0; 2];
        let mut times = [0; 3];

        {
            let mut stack = [0; 2];
            // SAFETY: the same target-header witness observes this owner task.
            if unsafe { nxrs_stream_worker_stack(stack.as_mut_ptr()) } == 0 {
                println!("NXRS_STREAM_STACK budget={} used={}", stack[0], stack[1]);
            }
        }
        // SAFETY: writable arrays match the two scalar output arrays in C.
        if unsafe { nxrs_stream_metrics(memory.as_mut_ptr(), times.as_mut_ptr()) } != 0 {
            return Err("resource measurement failed".into());
        }
        let Some(last) = previous.replace(times) else {
            return Ok(());
        };
        let elapsed_us = times[0]
            .checked_sub(last[0])
            .ok_or("measurement clock regressed")?;
        let idle_us = times[1]
            .checked_sub(last[1])
            .ok_or("idle counter regressed")?;
        if elapsed_us == 0 || idle_us > elapsed_us {
            return Err(format!(
                "invalid CPU accounting interval wall_us={elapsed_us} idle_us={idle_us}"
            ));
        }
        let busy = 1000 - idle_us * 1000 / elapsed_us;
        let stats = live.stats();
        let discarded = stats
            .camera_discarded
            .ok_or("camera discard accounting unavailable")?;
        println!(
            "NXRS_STREAM_REPORT ms={elapsed} captured={} delivered={} bytes={} max_bytes={} heap_user={} heap_kernel={} cpu_busy_pm={} owner_cpu_ms={} online={} attempts={attempts} rejoined={rejoined} errors={} camera_discarded={discarded}",
            stats.captured,
            stats.stream.frames_completed,
            stats.captured_bytes,
            stats.max_frame_bytes,
            memory[0],
            memory[1],
            busy,
            times[2] / 1000,
            u8::from(online),
            stats.stream.errors
        );
        if let Some(tls) = stats.tls {
            println!(
                "NXRS_TLS_REPORT ms={elapsed} captured={} delivered={} handshakes={} failures={} last={:?} heap_user={} heap_kernel={}",
                stats.captured,
                stats.stream.frames_completed,
                tls.handshakes,
                tls.failures,
                tls.last_failure,
                memory[0],
                memory[1]
            );
        }
        if let Some(detection) = live.detection() {
            let s = detection.snapshot();
            let (sequence, age_ms, score) = s.latest.map_or((0, 0, 0), |v| {
                (
                    v.sequence,
                    elapsed.saturating_sub(v.captured_ms),
                    v.score.permille(),
                )
            });
            println!(
                "NXRS_DETECTION_REPORT ms={elapsed} state={:?} submitted={} completed={} failures={} inference_us={} max_inference_us={} events={} sequence={sequence} age_ms={age_ms} score={score}",
                s.presence,
                s.worker.submitted,
                s.worker.completed,
                s.failures,
                s.worker.inference_us,
                s.worker.max_inference_us,
                s.event_count
            );
        }
        Ok(())
    }

    fn dispatch_events<W, F>(
        network: &mut Reconnector<W, F>,
        receiver: &Receiver<Event>,
        elapsed: u64,
    ) -> Result<(), String>
    where
        W: wifi::Wifi,
        F: FnMut() -> Result<Command, wifi::Error>,
    {
        for _ in 0..16 {
            match receiver.try_recv() {
                Ok(event) => network
                    .on_event(event, elapsed)
                    .map_err(|error| format!("wifi event {error:?}"))?,
                Err(std::sync::mpsc::TryRecvError::Empty) => break,
                Err(_) => return Err("wifi event channel closed".into()),
            }
        }
        Ok(())
    }

    fn wait_addressed<W, F>(
        network: &mut Reconnector<W, F>,
        receiver: &Receiver<Event>,
        origin: Instant,
    ) -> Result<(), String>
    where
        W: wifi::Wifi,
        F: FnMut() -> Result<Command, wifi::Error>,
    {
        let deadline = Instant::now() + Duration::from_secs(90);
        while Instant::now() < deadline {
            let elapsed = now(origin);
            dispatch_events(network, receiver, elapsed)?;
            network
                .poll(elapsed)
                .map_err(|error| format!("wifi policy {error:?}"))?;
            if network.link() == LinkState::Addressed {
                return Ok(());
            }
            std::thread::park_timeout(Duration::from_millis(5));
        }
        Err("Wi-Fi did not reach addressed state before TLS startup".into())
    }

    struct Options {
        duration: super::session::Duration,
        reconnect: u64,
        detection_enabled: bool,
        sample_interval_ms: u64,
        width: u16,
        height: u16,
        port: u16,
    }

    fn run_live<D, O, W, F>(
        mut live: LiveCamera<D, BYTES, Vec<u8>, O>,
        network: &mut Reconnector<W, F>,
        receiver: &Receiver<Event>,
        options: &Options,
        stop_control: Option<&AtomicU8>,
        origin: Instant,
    ) -> Result<(), String>
    where
        D: camera::Camera,
        O: StreamOutput,
        W: wifi::Wifi,
        F: FnMut() -> Result<Command, wifi::Error>,
    {
        let duration = &options.duration;
        let (reconnect, detection_enabled, sample_interval_ms) = (
            options.reconnect,
            options.detection_enabled,
            options.sample_interval_ms,
        );
        let (width, height, port) = (options.width, options.height, options.port);
        let result: Result<(), String> = (|| {
            if detection_enabled {
                use detection_service::{Error, Policy, Worker, WorkerOptions};
                stage("detection_spawn");
                let worker = Worker::spawn(
                    WorkerOptions {
                        frame_capacity: BYTES,
                        sample_interval_ms,
                        stack_bytes: super::model::STACK_BYTES,
                    },
                    || {
                        // SAFETY: target-header scheduler call affects this worker only.
                        if unsafe { nxrs_stream_worker_priority(90) } != 0 {
                            return Err(Error::Worker);
                        }
                        stage("detection_worker");
                        report_model_stack();
                        stage("detection_person");
                        let model = super::model::open()?;
                        let mut detector = QualifiedDetector(model);
                        let person = detector::Detector::detect(
                            &mut detector,
                            detector::Image::jpeg(
                                include_bytes!("../../detection/person.jpg"),
                                96,
                                96,
                            ),
                        )?
                        .person_score;
                        stage("detection_background");
                        let background = detector::Detector::detect(
                            &mut detector,
                            detector::Image::jpeg(
                                include_bytes!("../../detection/background.jpg"),
                                96,
                                96,
                            ),
                        )?
                        .person_score;
                        if person.permille() < 500 || background.permille() >= 500 {
                            return Err(Error::Model);
                        }
                        println!(
                            "NXRS_DETECTION_WITNESS person={} background={}",
                            person.permille(),
                            background.permille()
                        );
                        report_model_stack();
                        Ok(detector)
                    },
                )
                .map_err(|error| format!("detection worker {error:?}"))?;
                let detection = camera_stream::SecurityDetection::new(worker, Policy::default())
                    .map_err(|error| format!("detection composition {error:?}"))?;
                live.attach_detection(detection)
                    .map_err(|error| format!("detection attach {error:?}"))?;
                println!("NXRS_DETECTION_ENABLED sample_ms={sample_interval_ms}");
            }
            stage("capture_start");
            live.start(width, height)
                .map_err(|error| format!("camera start {error:?}"))?;
            stage("running");
            let mut was_online = false;
            let mut did_reconnect = reconnect == 0;
            let started = now(origin);
            let (mut last_report, mut last_capture, mut captured) = (started, started, 0);
            let mut reported_errors = 0;
            let mut measurement = None;
            let mut control = super::session::Control::default();
            report(&live, 0, false, 0, 0, &mut measurement)?;
            loop {
                let elapsed = now(origin);
                if duration.expired(elapsed.saturating_sub(started)) {
                    break;
                }
                if stop_control.is_some_and(|flag| flag.load(Ordering::Acquire) != 0) {
                    break;
                }
                if stop_control.is_none() && *duration == super::session::Duration::Continuous {
                    // TLS mode gives the sole UART reader to the TLS worker.
                    // Plain HTTP retains its existing bounded stop parser.
                    let ready = unsafe { nxrs_stream_console_ready() };
                    if ready > 0 {
                        let mut input = [0; 16];
                        if let Ok(count) = std::io::stdin().read(&mut input) {
                            if control.input(&input[..count]) {
                                break;
                            }
                        }
                    }
                }
                dispatch_events(network, receiver, elapsed)?;
                network
                    .poll(elapsed)
                    .map_err(|error| format!("wifi policy {error:?}"))?;
                let online = network.link() == LinkState::Addressed;
                if online && !was_online {
                    if stop_control.is_some() {
                        println!("NXRS_TLS_LISTEN ip={} port={port}", address()?);
                    } else {
                        println!(
                            "NXRS_STREAM_ONLINE ip={} port={port} ms={elapsed}",
                            address()?
                        );
                    }
                }
                if !online && was_online {
                    println!("NXRS_STREAM_OFFLINE ms={elapsed}");
                }
                was_online = online;
                live.poll(elapsed, online)
                    .map_err(|error| format!("stream {error:?}"))?;
                let stats = live.stats();
                if stats.stream.errors != reported_errors {
                    println!(
                        "NXRS_STREAM_ERROR ms={elapsed} reason={:?}",
                        stats.stream.last_failure
                    );
                    reported_errors = stats.stream.errors;
                }
                if stats.captured != captured {
                    captured = stats.captured;
                    last_capture = elapsed;
                }
                if elapsed.saturating_sub(last_capture) > 5000 {
                    return Err("camera stalled".into());
                }
                if !did_reconnect && elapsed.saturating_sub(started) >= reconnect * 1000 {
                    network
                        .request_reconnect(elapsed)
                        .map_err(|error| format!("reconnect {error:?}"))?;
                    did_reconnect = true;
                    println!("NXRS_STREAM_RECONNECT_REQUEST ms={elapsed}");
                }
                if elapsed.saturating_sub(last_report) >= 10_000 {
                    report(
                        &live,
                        elapsed,
                        online,
                        network.stats().attempts,
                        network.stats().reconnections,
                        &mut measurement,
                    )?;
                    last_report = elapsed;
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
            report(
                &live,
                now(origin),
                was_online,
                network.stats().attempts,
                network.stats().reconnections,
                &mut measurement,
            )?;
            let stats = live.stats();
            if stats.captured == 0 || stats.stream.frames_completed == 0 {
                return Err("no delivered frames".into());
            }
            if reconnect != 0 && network.stats().reconnections == 0 {
                return Err("link recovery not observed".into());
            }
            if let Some(tls) = stats.tls {
                if tls.handshakes == 0 {
                    return Err("no authenticated TLS session".into());
                }
            }
            Ok(())
        })();
        let stopped = live
            .stop()
            .map_err(|error| format!("camera stop {error:?}"));
        result?;
        stopped?;
        if let Some(tls) = live.stats().tls {
            println!(
                "NXRS_TLS_DELIVERED captured={} delivered={} handshakes={} failures={}",
                live.stats().captured,
                live.stats().stream.frames_completed,
                tls.handshakes,
                tls.failures
            );
        }
        Ok(())
    }

    pub fn run() -> Result<(), String> {
        let mut arguments: Vec<String> = std::env::args().skip(1).collect();
        if let Some(result) = super::model::batch_command(&arguments) {
            return result;
        }
        let tls_mode = arguments.first().is_some_and(|value| value == "tls");
        if tls_mode {
            arguments.remove(0);
        }
        let (arguments, detection_enabled, sample_interval_ms) = {
            let mut arguments = arguments;
            let interval = arguments
                .last()
                .map(|value| super::session::detection_interval(value))
                .transpose()?
                .flatten();
            if interval.is_some() {
                arguments.pop();
            }
            let requested = arguments.last().is_none_or(|v| v != "no-detection");
            let enabled = requested && super::model::ENABLED;
            if !requested {
                arguments.pop();
            }
            (
                arguments,
                enabled,
                interval.unwrap_or(super::model::SAMPLE_MS),
            )
        };
        if !matches!(arguments.len(), 2 | 4 | 5) {
            return Err("usage: stream_test [tls] SECONDS|continuous RECONNECT_AT_SECONDS [WIDTH HEIGHT [QUALITY]] [no-detection] [sample-ms=N]".into());
        }
        let reconnect: u64 = arguments[1].parse().map_err(|_| "reconnect time")?;
        let duration = super::session::Duration::parse(&arguments[0], reconnect)?;
        if tls_mode && duration != super::session::Duration::Continuous {
            return Err("TLS mode requires continuous operation".into());
        }
        let (width, height) = if arguments.len() >= 4 {
            (
                arguments[2].parse().map_err(|_| "width")?,
                arguments[3].parse().map_err(|_| "height")?,
            )
        } else {
            (320u16, 240u16)
        };
        if !matches!((width, height), (320, 240) | (640, 480)) {
            return Err("supported JPEG sizes are 320x240 and 640x480".into());
        }
        let quality = JpegQuality::new(if arguments.len() == 5 {
            arguments[4].parse().map_err(|_| "JPEG quality")?
        } else {
            81
        })
        .map_err(|_| "JPEG quality must be 1..100")?;

        stage("credential_reader");
        let profile = Profile::read()?;
        let (sender, receiver) = sync_channel(8);
        stage("wifi_open");
        let controller = wifi::open(Sink {
            events: sender,
            owner: std::thread::current(),
        })
        .map_err(|error| format!("wifi open {error:?}"))?;
        let mut network = Reconnector::new(controller, || profile.command());
        let origin = Instant::now();
        let tls_stop = Arc::new(AtomicU8::new(0));
        let operation: Result<(), String> = (|| {
            if tls_mode {
                stage("wifi_preflight");
                wait_addressed(&mut network, &receiver, origin)?;
            }
            stage("tcp_bind");
            let port = if tls_mode { 8443 } else { 8080 };
            let listener = TcpListener::bind(("0.0.0.0", port))
                .map_err(|error| format!("listener {error:?}"))?;
            let options = Options {
                duration,
                reconnect,
                detection_enabled,
                sample_interval_ms,
                width,
                height,
                port,
            };
            if tls_mode {
                loop {
                    stage("camera_open");
                    let mut device =
                        camera::open().map_err(|error| format!("camera open {error:?}"))?;
                    device
                        .set_jpeg_quality(quality)
                        .map_err(|error| format!("JPEG quality {error:?}"))?;
                    stage("pipeline_setup");
                    let stop = tls_stop.clone();
                    let live = LiveCamera::<_, BYTES, _>::new_tls(
                        device,
                        listener
                            .try_clone()
                            .map_err(|error| format!("listener clone {error:?}"))?,
                        std::array::from_fn(|_| vec![0; BYTES]),
                        stream_service::Options::default(),
                        TlsOptions {
                            stack_bytes: 64 * 1024,
                            poll_interval: Duration::from_millis(2),
                        },
                        move || {
                            // Keep model observations schedulable during encryption.
                            // SAFETY: target-header call affects this TLS worker only.
                            if unsafe { nxrs_stream_worker_priority(80) } != 0 {
                                return Err(camera_stream::Error::Worker);
                            }
                            super::console::initialize(stop)
                        },
                    )
                    .map_err(|error| format!("TLS composition {error:?}"))?;
                    run_live(
                        live,
                        &mut network,
                        &receiver,
                        &options,
                        Some(&tls_stop),
                        origin,
                    )?;
                    match tls_stop.load(Ordering::Acquire) {
                        1 => break,
                        2 => {
                            tls_stop.store(0, Ordering::Release);
                            println!("NXRS_TLS_SESSION_PASS stopped=1 joined=1");
                        }
                        value => {
                            return Err(format!("invalid TLS session control {value}"));
                        }
                    }
                }
            } else {
                stage("camera_open");
                let mut device =
                    camera::open().map_err(|error| format!("camera open {error:?}"))?;
                device
                    .set_jpeg_quality(quality)
                    .map_err(|error| format!("JPEG quality {error:?}"))?;
                stage("pipeline_setup");
                let live = LiveCamera::<_, BYTES, _>::new(
                    device,
                    listener,
                    vec![0; BYTES],
                    vec![0; BYTES],
                    vec![0; BYTES],
                    stream_service::Options::default(),
                )
                .map_err(|error| format!("composition {error:?}"))?;
                run_live(live, &mut network, &receiver, &options, None, origin)?;
            }
            Ok(())
        })();
        let stop_result = network
            .request_disconnect_and_stop(now(origin))
            .map_err(|error| format!("wifi stop {error:?}"));
        let deadline = Instant::now() + Duration::from_secs(35);
        while network.status() != ProviderStatus::Stopped && Instant::now() < deadline {
            let elapsed = now(origin);
            let _ = dispatch_events(&mut network, &receiver, elapsed);
            let _ = network.poll(elapsed);
            std::thread::park_timeout(Duration::from_millis(5));
        }
        let joined = network
            .finish()
            .map_err(|error| format!("wifi join {error:?}"));
        operation?;
        stop_result?;
        joined?;
        if tls_mode {
            println!("NXRS_TLS_CAMERA_PASS stopped=1 joined=1");
        } else {
            println!("NXRS_STREAM_PASS stopped=1 joined=1");
        }
        Ok(())
    }
}

#[cfg(target_os = "nuttx")]
fn main() {
    if let Err(error) = device::run() {
        if std::env::args().nth(1).as_deref() == Some("tls") {
            eprintln!("NXRS_TLS_CAMERA_FAIL error={error}");
        } else {
            eprintln!("NXRS_STREAM_FAIL error={error}");
        }
        std::process::exit(1);
    }
}

#[cfg(not(target_os = "nuttx"))]
fn main() {
    eprintln!("physical stream fixture requires a NuttX camera + Wi-Fi platform");
    std::process::exit(2);
}
