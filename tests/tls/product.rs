//! Real product composition with synthetic JPEG capture and fixture PKI/UTC.
use super::*;
use camera::{Camera, Capture, DeviceError, Format};
use camera_stream::{LiveCamera, TlsIdentity, TlsOptions};
use std::{cell::Cell, rc::Rc};

struct Source {
    sequence: u64,
    format: Option<Format>,
    root: PathBuf,
    images: Option<[Vec<u8>; 2]>,
    paused: Rc<Cell<bool>>,
    next_ms: u64,
}
impl Drop for Source {
    fn drop(&mut self) {
        let _identity = tls::open().expect("TLS output retires before camera storage");
    }
}
impl Camera for Source {
    fn start(&mut self, format: Format) -> Result<Format, DeviceError> {
        std::fs::write(self.root.join("capture-started"), b"").unwrap();
        self.format = Some(format);
        Ok(format)
    }
    fn poll_frame(&mut self, now: u64, bytes: &mut [u8]) -> Result<Option<Capture>, DeviceError> {
        if self.paused.get() || now < self.next_ms {
            return Ok(None);
        }
        self.next_ms = now + if self.images.is_some() { 100 } else { 0 };
        self.sequence += 1;
        let jpeg: &[u8] = if let Some(images) = &self.images {
            &images[((self.sequence - 1) % 2) as usize]
        } else if self.sequence & 1 == 1 {
            include_bytes!("../nuttx-std/detection/background.jpg")
        } else {
            include_bytes!("../nuttx-std/detection/person.jpg")
        };
        bytes[..jpeg.len()].copy_from_slice(jpeg);
        Ok(Some(Capture {
            format: self.format.ok_or(DeviceError::Io)?,
            len: jpeg.len(),
            timestamp_ms: now,
        }))
    }
    fn stop(&mut self) -> Result<(), DeviceError> {
        self.format = None;
        if self.root.join("fail-camera-stop").exists() {
            Err(DeviceError::Io)
        } else {
            Ok(())
        }
    }
}
pub(super) fn run(directory: PathBuf, age: u64, stack_bytes: usize, case: Case) {
    let listener = std::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).unwrap();
    let address = listener.local_addr().unwrap();
    let root = directory.clone();
    let media = matches!(case, Case::Media);
    let images = media.then(|| {
        [
            public(root.join("rust-frame-1.jpg")),
            public(root.join("rust-frame-2.jpg")),
        ]
    });
    let paused = Rc::new(Cell::new(false));
    let live = LiveCamera::<_, 16384, _>::new_tls(
        Source {
            sequence: 0,
            format: None,
            root: root.clone(),
            images,
            paused: paused.clone(),
            next_ms: 0,
        },
        listener,
        std::array::from_fn(|_| vec![0; 16384]),
        stream_service::Options::default(),
        TlsOptions {
            stack_bytes,
            poll_interval: Duration::from_millis(2),
        },
        move || {
            let mut identity = tls::open().map_err(camera_stream::Error::Tls)?;
            let mut csr = [0; 1024];
            let size = identity.request("fixture", &mut csr).unwrap();
            std::fs::write(root.join("request.csr"), &csr[..size]).unwrap();
            println!("CSR_READY");
            std::io::stdout().flush().unwrap();
            let mut line = String::new();
            std::io::stdin().read_line(&mut line).unwrap();
            assert_eq!(line.trim(), "load");
            // This explicitly non-Send closure, like the key, stays on the worker.
            let local = std::rc::Rc::new(());
            let time_root = root.clone();
            Ok(TlsIdentity {
                identity,
                policy: policy(&root),
                own: public(root.join("server.der")),
                issuer: public(root.join("issuer.der")),
                approved_peer: public(root.join("peer.der")),
                revocations: public(root.join("revocations.der")),
                time: move || {
                    let _ = &local;
                    if time_root.join("block-time").exists() {
                        println!("TIME_BLOCKED");
                        std::io::stdout().flush().unwrap();
                        let deadline = Instant::now() + Duration::from_secs(5);
                        while time_root.join("block-time").exists() && Instant::now() < deadline {
                            std::thread::sleep(Duration::from_millis(2));
                        }
                    }
                    if time_root.join("time-error").exists() {
                        return Err(tls::Error::Time);
                    }
                    tls::TimeLease::from_trusted(
                        SystemTime::now()
                            .duration_since(UNIX_EPOCH)
                            .unwrap()
                            .as_secs(),
                        Duration::from_millis(age),
                    )
                },
            })
        },
    );
    let mut live = match live {
        Ok(live) => live,
        Err(error) => {
            drop(tls::open().expect("failed startup released SDK owner"));
            println!("STARTUP Err({error:?})");
            return;
        }
    };
    if media {
        // Exercise real publication/transport with a deliberately synthetic box.
        // This is not model-quality or compute-performance evidence.
        struct Synthetic;
        impl detector::Detector for Synthetic {
            fn capabilities(&self) -> detector::Capabilities {
                detector::Capabilities::localized(96, 96)
            }
            fn detect(
                &mut self,
                _: detector::Image<'_>,
            ) -> Result<detector::DetectionResult, detector::DeviceError> {
                let score = detector::Score::new(900)?;
                let mut objects = detector::Detections::default();
                objects.boxes[0] = Some(detector::Detection {
                    score,
                    bounds: detector::Bounds {
                        left: 100,
                        top: 100,
                        right: 900,
                        bottom: 900,
                    },
                });
                Ok(detector::DetectionResult {
                    person_score: score,
                    objects,
                })
            }
        }
        let worker = detection_service::Worker::spawn(
            detection_service::WorkerOptions {
                frame_capacity: 16384,
                sample_interval_ms: 100,
                stack_bytes: 65536,
            },
            || Ok(Synthetic),
        )
        .unwrap();
        live.attach_detection(
            camera_stream::SecurityDetection::new(worker, detection_service::Policy::default())
                .unwrap(),
        )
        .unwrap();
    }
    let (width, height) = if media { (320, 240) } else { (96, 96) };
    live.start(width, height).unwrap();
    assert!(!camera_stream::startup::healthy(&live, 0, 5000));
    println!("LISTEN {address}");
    std::io::stdout().flush().unwrap();
    let origin = Instant::now();
    let mut online = true;
    let mut report_at = 0;
    let mut drop_only = false;
    let mut confirm = false;
    let mut peak = 0;
    while origin.elapsed() < Duration::from_secs(if media { 120 } else { 30 }) {
        match std::fs::read_to_string(directory.join("control"))
            .unwrap_or_default()
            .trim()
        {
            "offline" if online => {
                online = false;
                println!("OFFLINE");
                std::io::stdout().flush().unwrap();
            }
            "online" if !online => {
                online = true;
                println!("ONLINE");
                std::io::stdout().flush().unwrap();
            }
            "stop" => break,
            "confirm" => {
                confirm = true;
            }
            "pause" => {
                paused.set(true);
                std::fs::write(directory.join("control"), "").unwrap();
                println!("PAUSED");
                std::io::stdout().flush().unwrap();
            }
            "resume" => {
                paused.set(false);
                std::fs::write(directory.join("control"), "").unwrap();
                println!("RESUMED");
                std::io::stdout().flush().unwrap();
            }
            "drop" => {
                drop_only = true;
                break;
            }
            _ => {}
        }
        let now = origin.elapsed().as_millis() as u64;
        live.poll(now, online).unwrap();
        if confirm && camera_stream::startup::healthy(&live, now, 5000) {
            break;
        }
        let stats = live.stats();
        let active = stats
            .stream
            .accepted
            .checked_sub(stats.stream.disconnected)
            .unwrap();
        assert!(active <= 1, "one admitted TLS camera consumer");
        peak = peak.max(active);
        if now >= report_at {
            report(&directory, stats, peak);
            report_at = now + 50;
        }
        std::thread::sleep(Duration::from_millis(2));
    }
    if confirm {
        let now = origin.elapsed().as_millis() as u64;
        assert!(camera_stream::startup::healthy(&live, now, 5000));
        report(&directory, live.stats(), peak);
        let _health = camera_stream::startup::retire_healthy(live, now, 5000)
            .expect("real authenticated image and joined retirement");
        println!("STARTUP_RETIRED");
    } else {
        if !drop_only {
            let result = live.stop();
            assert_eq!(result.is_err(), directory.join("fail-camera-stop").exists());
            report(&directory, live.stats(), peak);
        }
        drop(live);
    }
    // Even a failed camera stop must have joined TLS and released its SDK owner.
    let mut replacement = tls::open().expect("TLS worker joined and released native owner");
    let mut csr = [0; 1024];
    let count = replacement.request("replacement", &mut csr).unwrap();
    std::fs::write(directory.join("replacement.csr"), &csr[..count]).unwrap();
    println!("CAMERA_STOPPED");
    std::io::stdout().flush().unwrap();
}

fn report(directory: &Path, stats: camera_stream::Stats, peak: u64) {
    let active = stats
        .stream
        .accepted
        .checked_sub(stats.stream.disconnected)
        .unwrap();
    let tls = stats.tls.unwrap();
    let state = format!("{{\"captured\":{},\"accepted\":{},\"disconnected\":{},\"frames\":{},\"handshakes\":{},\"failures\":{},\"busy\":{},\"active\":{},\"peak\":{},\"rejected_busy\":{}}}",
                stats.captured, stats.stream.accepted, stats.stream.disconnected, stats.stream.frames_completed,
                tls.handshakes, tls.failures, tls.skipped_busy, active, peak, stats.stream.rejected_busy);
    std::fs::write(directory.join("state.next"), state).unwrap();
    std::fs::rename(directory.join("state.next"), directory.join("state.json")).unwrap();
}
