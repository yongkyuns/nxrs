//! Physical qualification composition, not a hardware-specific streaming app.
#[cfg(target_os = "nuttx")]
mod device {
    use camera::{Camera, JpegQuality};
    use camera_stream::LiveCamera;
    use connectivity_service::Reconnector;
    use std::net::{Ipv4Addr, TcpListener};
    use std::sync::mpsc::{sync_channel, SyncSender, TrySendError};
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

    fn address() -> Result<Ipv4Addr, String> {
        let mut bytes = [0; 4];
        // SAFETY: writable four-byte IPv4 output, queried with native headers.
        if unsafe { nxrs_stream_address(bytes.as_mut_ptr()) } != 0 {
            return Err("no station IPv4 address".into());
        }
        Ok(Ipv4Addr::from(bytes))
    }

    fn report<D: camera::Camera>(
        live: &LiveCamera<D, BYTES, Vec<u8>>,
        elapsed: u64,
        online: bool,
        attempts: u64,
        rejoined: u64,
        previous: &mut Option<[u64; 3]>,
    ) -> Result<(), String> {
        let mut memory = [0; 2];
        let mut times = [0; 3];
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
        println!("NXRS_STREAM_REPORT ms={elapsed} captured={} delivered={} bytes={} max_bytes={} heap_user={} heap_kernel={} cpu_busy_pm={} owner_cpu_ms={} online={} attempts={attempts} rejoined={rejoined} errors={}",
            stats.captured, stats.stream.frames_completed, stats.captured_bytes, stats.max_frame_bytes,
            memory[0], memory[1], busy, times[2] / 1000, u8::from(online), stats.stream.errors);
        Ok(())
    }

    pub fn run() -> Result<(), String> {
        let arguments: Vec<String> = std::env::args().skip(1).collect();
        if !matches!(arguments.len(), 2 | 4 | 5) {
            return Err(
                "usage: stream_test SECONDS RECONNECT_AT_SECONDS [WIDTH HEIGHT [QUALITY]]".into(),
            );
        }
        let seconds: u64 = arguments[0].parse().map_err(|_| "duration")?;
        let reconnect: u64 = arguments[1].parse().map_err(|_| "reconnect time")?;
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
            81 // Preserves this platform's existing quantization scale 12.
        })
        .map_err(|_| "JPEG quality must be 1..100")?;
        if !(10..=3600).contains(&seconds) || reconnect >= seconds {
            return Err("invalid duration/reconnect time".into());
        }
        stage("credential_reader");
        let profile = Profile::read()?;
        stage("camera_open");
        let mut device = camera::open().map_err(|error| format!("camera open {error:?}"))?;
        device
            .set_jpeg_quality(quality)
            .map_err(|error| format!("JPEG quality {error:?}"))?;
        stage("tcp_bind");
        let listener =
            TcpListener::bind("0.0.0.0:8080").map_err(|error| format!("listener {error:?}"))?;
        stage("pipeline_setup");
        let mut live = LiveCamera::<_, BYTES, _>::new(
            device,
            listener,
            vec![0; BYTES],
            vec![0; BYTES],
            vec![0; BYTES],
            stream_service::Options::default(),
        )
        .map_err(|error| format!("composition {error:?}"))?;
        stage("capture_start");
        live.start(width, height)
            .map_err(|error| format!("camera start {error:?}"))?;
        let (sender, receiver) = sync_channel(8);
        stage("wifi_open");
        let controller = match wifi::open(Sink {
            events: sender,
            owner: std::thread::current(),
        }) {
            Ok(controller) => controller,
            Err(error) => {
                let _ = live.stop();
                return Err(format!("wifi open {error:?}"));
            }
        };
        let mut network = Reconnector::new(controller, || profile.command());
        stage("running");
        let origin = Instant::now();
        let mut was_online = false;
        let mut did_reconnect = reconnect == 0;
        let (mut last_report, mut last_capture, mut captured) = (0, 0, 0);
        let mut reported_errors = 0;
        let mut measurement = None;
        let result: Result<(), String> = (|| {
            report(&live, 0, false, 0, 0, &mut measurement)?;
            loop {
                let elapsed = now(origin);
                if elapsed >= seconds * 1000 {
                    break;
                }
                // Bound queue dispatch even if the worker is producing events.
                for _ in 0..16 {
                    match receiver.try_recv() {
                        Ok(event) => network
                            .on_event(event, elapsed)
                            .map_err(|error| format!("wifi event {error:?}"))?,
                        Err(std::sync::mpsc::TryRecvError::Empty) => break,
                        Err(_) => return Err("wifi event channel closed".into()),
                    }
                }
                network
                    .poll(elapsed)
                    .map_err(|error| format!("wifi policy {error:?}"))?;
                let online = network.link() == LinkState::Addressed;
                if online && !was_online {
                    println!(
                        "NXRS_STREAM_ONLINE ip={} port=8080 ms={elapsed}",
                        address()?
                    );
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
                if !did_reconnect && elapsed >= reconnect * 1000 {
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
                // One requested wait of at most 5 ms, woken early by Wi-Fi;
                // actual dispatch also depends on OS resolution and load.
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
            if captured == 0 || live.stats().stream.frames_completed == 0 {
                return Err("no delivered frames".into());
            }
            if reconnect != 0 && network.stats().reconnections == 0 {
                return Err("link recovery not observed".into());
            }
            Ok(())
        })();
        let stopped = live
            .stop()
            .map_err(|error| format!("camera stop {error:?}"));
        let stop_result = network
            .request_disconnect_and_stop(now(origin))
            .map_err(|error| format!("wifi stop {error:?}"));
        let deadline = Instant::now() + Duration::from_secs(35);
        // Drain even after an error: callbacks must quiesce before joining.
        while network.status() != ProviderStatus::Stopped && Instant::now() < deadline {
            for _ in 0..16 {
                let Ok(event) = receiver.try_recv() else {
                    break;
                };
                let _ = network.on_event(event, now(origin));
            }
            let _ = network.poll(now(origin));
            std::thread::park_timeout(Duration::from_millis(5));
        }
        let joined = network
            .finish()
            .map_err(|error| format!("wifi join {error:?}"));
        result?;
        stopped?;
        stop_result?;
        joined?;
        println!("NXRS_STREAM_PASS stopped=1 joined=1");
        Ok(())
    }
}

#[cfg(target_os = "nuttx")]
fn main() {
    if let Err(error) = device::run() {
        eprintln!("NXRS_STREAM_FAIL error={error}");
        std::process::exit(1);
    }
}

#[cfg(not(target_os = "nuttx"))]
fn main() {
    eprintln!("physical stream fixture requires a NuttX camera + Wi-Fi platform");
    std::process::exit(2);
}
