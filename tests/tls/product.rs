//! Real product composition with synthetic JPEG capture and fixture PKI/UTC.
use super::*;
use camera::{Camera, Capture, DeviceError, Format};
use camera_stream::{LiveCamera, TlsIdentity, TlsOptions};

struct Source {
    sequence: u64,
    format: Option<Format>,
    root: PathBuf,
}
impl Drop for Source {
    fn drop(&mut self) {
        let _identity = tls::open().expect("TLS output retires before camera storage");
    }
}
impl Camera for Source {
    fn start(&mut self, format: Format) -> Result<Format, DeviceError> {
        self.format = Some(format);
        Ok(format)
    }
    fn poll_frame(&mut self, now: u64, bytes: &mut [u8]) -> Result<Option<Capture>, DeviceError> {
        self.sequence += 1;
        let jpeg: &[u8] = if self.sequence & 1 == 1 {
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
pub fn run(directory: PathBuf, age: u64) {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let root = directory.clone();
    let mut live = LiveCamera::<_, 16384, _>::new_tls(
        Source {
            sequence: 0,
            format: None,
            root: root.clone(),
        },
        listener,
        std::array::from_fn(|_| vec![0; 16384]),
        stream_service::Options::default(),
        TlsOptions {
            stack_bytes: 65536,
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
    )
    .expect("product TLS startup");
    live.start(96, 96).unwrap();
    println!("LISTEN {address}");
    std::io::stdout().flush().unwrap();
    let origin = Instant::now();
    let mut online = true;
    let mut report_at = 0;
    let mut drop_only = false;
    while origin.elapsed() < Duration::from_secs(30) {
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
            "drop" => {
                drop_only = true;
                break;
            }
            _ => {}
        }
        let now = origin.elapsed().as_millis() as u64;
        live.poll(now, online).unwrap();
        if now >= report_at {
            let stats = live.stats();
            let tls = stats.tls.unwrap();
            let state = format!("{{\"captured\":{},\"accepted\":{},\"disconnected\":{},\"frames\":{},\"handshakes\":{},\"failures\":{},\"busy\":{}}}",
                stats.captured, stats.stream.accepted, stats.stream.disconnected, stats.stream.frames_completed,
                tls.handshakes, tls.failures, tls.skipped_busy);
            std::fs::write(directory.join("state.next"), state).unwrap();
            std::fs::rename(directory.join("state.next"), directory.join("state.json")).unwrap();
            report_at = now + 50;
        }
        std::thread::sleep(Duration::from_millis(2));
    }
    if !drop_only {
        let result = live.stop();
        assert_eq!(result.is_err(), directory.join("fail-camera-stop").exists());
    }
    drop(live);
    // Even a failed camera stop must have joined TLS and released its SDK owner.
    let mut replacement = tls::open().expect("TLS worker joined and released native owner");
    let mut csr = [0; 1024];
    let count = replacement.request("replacement", &mut csr).unwrap();
    std::fs::write(directory.join("replacement.csr"), &csr[..count]).unwrap();
    println!("CAMERA_STOPPED");
    std::io::stdout().flush().unwrap();
}
