//! Host fixture: actual SDK key/TLS + actual camera composition/HTTP framing.
//! Only image/entropy/issuer inputs are synthetic; no private camera key export.
#![forbid(unsafe_code)]
use camera::{Camera, Capture, DeviceError, Format};
use camera_stream::ThreadedCamera;
use device_identity::{
    CameraCertificatePolicy, Entropy, EntropyUnavailable, Identity, RevocationState,
    TlsCancellation, TlsError, TlsPolicy,
};
use std::{
    env,
    fs::{File, OpenOptions},
    io::{self, Read, Write},
    net::TcpListener,
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    },
    thread,
    time::{Duration, Instant},
};
use stream_service::{Options, Server, StreamWorker, WorkerOptions};
fn invalid() -> io::Error {
    io::ErrorKind::InvalidData.into()
}
fn bytes(path: &Path, max: usize) -> io::Result<Vec<u8>> {
    let mut output = Vec::new();
    File::open(path)?
        .take((max + 1) as u64)
        .read_to_end(&mut output)?;
    if output.is_empty() || output.len() > max {
        return Err(invalid());
    }
    Ok(output)
}
fn integer(path: &Path) -> io::Result<u64> {
    let raw = bytes(path, 20)?;
    let text = std::str::from_utf8(&raw).map_err(|_| invalid())?;
    if !text.bytes().all(|b| b.is_ascii_digit()) {
        return Err(invalid());
    }
    text.parse().map_err(|_| invalid())
}
struct OsEntropy(File);
impl Entropy for OsEntropy {
    fn fill(&mut self, out: &mut [u8]) -> Result<(), EntropyUnavailable> {
        self.0.read_exact(out).map_err(|_| EntropyUnavailable)
    }
}
struct Images {
    images: [Vec<u8>; 2],
    format: Option<Format>,
    paused: Arc<AtomicBool>,
    next: u64,
    index: usize,
}
impl Camera for Images {
    fn start(&mut self, format: Format) -> Result<Format, DeviceError> {
        self.format = Some(format);
        Ok(format)
    }
    fn poll_frame(&mut self, now: u64, out: &mut [u8]) -> Result<Option<Capture>, DeviceError> {
        if self.paused.load(Ordering::Acquire) || now < self.next {
            return Ok(None);
        }
        let image = &self.images[self.index];
        if image.len() > out.len() {
            return Err(DeviceError::Full);
        }
        out[..image.len()].copy_from_slice(image);
        self.index ^= 1;
        self.next = now.saturating_add(100);
        Ok(Some(Capture {
            format: self.format.ok_or(DeviceError::Io)?,
            len: image.len(),
            timestamp_ms: now,
        }))
    }
    fn stop(&mut self) -> Result<(), DeviceError> {
        self.format = None;
        Ok(())
    }
}
fn controls(send: mpsc::SyncSender<String>) -> io::Result<()> {
    let mut input = io::stdin().lock();
    let mut line = [0; 16];
    let mut used = 0;
    loop {
        let mut byte = [0];
        if input.read(&mut byte)? == 0 {
            let _ = send.send("eof".into());
            return Ok(());
        }
        if byte[0] == b'\n' {
            let text = std::str::from_utf8(&line[..used]).map_err(|_| invalid())?;
            if !matches!(text, "enrolled" | "status" | "pause" | "resume" | "quit") {
                return Err(invalid());
            }
            send.send(text.into()).map_err(|_| invalid())?;
            used = 0;
            if text == "quit" {
                return Ok(());
            }
        } else {
            if used == line.len() {
                return Err(invalid());
            }
            line[used] = byte[0];
            used += 1;
        }
    }
}
fn run() -> Result<(), Box<dyn std::error::Error>> {
    let paths: Vec<_> = env::args_os().skip(1).collect();
    if paths.len() != 2 {
        return Err(invalid().into());
    }
    let images = [
        bytes(Path::new(&paths[0]), 153600)?,
        bytes(Path::new(&paths[1]), 153600)?,
    ];
    let directory = PathBuf::from(env::var_os("NXRS_DEVICE_TLS_FIXTURE_DIR").ok_or_else(invalid)?);
    let scope = String::from_utf8(bytes(&directory.join("scope"), 256)?)?;
    let ids: Vec<_> = scope.lines().map(str::to_owned).collect();
    if ids.len() != 3
        || ids.iter().any(|s| {
            s.is_empty()
                || s.len() > 64
                || !s
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
        })
    {
        return Err(invalid().into());
    }
    let (ready_send, ready) = mpsc::sync_channel(1);
    let (enroll_send, enroll) = mpsc::sync_channel(1);
    let epoch = Instant::now();
    let worker_directory = directory.clone();
    let worker = StreamWorker::spawn(
        vec![0; 153600],
        WorkerOptions {
            stack_bytes: 1024 * 1024,
            poll_interval: Duration::from_millis(5),
        },
        move |mut frames, stop| {
            let map = |e| io::Error::from(TlsError::from(e));
            let mut identity =
                Identity::generate(OsEntropy(File::open("/dev/urandom")?)).map_err(map)?;
            let mut csr = [0; 2048];
            let n = identity.request(&ids[2], &mut csr).map_err(map)?;
            OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(worker_directory.join("device.csr"))?
                .write_all(&csr[..n])?;
            csr.fill(0);
            ready_send.send(None).map_err(|_| invalid())?;
            enroll
                .recv_timeout(Duration::from_secs(15))
                .map_err(|_| invalid())?;
            let root = bytes(&worker_directory.join("root.der"), 2048)?;
            let leaf = bytes(&worker_directory.join("camera.der"), 2048)?;
            let crl = bytes(&worker_directory.join("revocations.der"), 32768)?;
            let pin: [u8; 32] = bytes(&worker_directory.join("pin.bin"), 32)?
                .try_into()
                .map_err(|_| invalid())?;
            let floor = integer(&worker_directory.join("generation"))?;
            let utc = integer(&worker_directory.join("utc"))?;
            if utc == 0 || utc > 253402300799 {
                return Err(invalid());
            }
            let observed = Instant::now();
            let state = RevocationState::from_trusted_floor(pin, floor).map_err(map)?;
            let policy = || CameraCertificatePolicy {
                issuer_sha256: pin,
                site_id: &ids[0],
                camera_id: &ids[2],
                server_ip: [127, 0, 0, 1],
                trusted_unix_seconds: utc + observed.elapsed().as_secs(),
            };
            let listener = TcpListener::bind("127.0.0.1:0")?;
            let port = listener.local_addr()?.port();
            let mut server = Server::with_transport(
                listener,
                vec![0; 153600],
                Options {
                    request_timeout_ms: 10000,
                    ..Options::default()
                },
                |socket| {
                    identity
                        .bind_current_certificate(&leaf, &root, policy(), &state, &crl)
                        .map_err(map)?
                        .tls_server_tcp(
                            socket,
                            &root,
                            TlsPolicy {
                                camera: policy(),
                                gateway_id: &ids[1],
                                handshake_timeout: Duration::from_secs(10),
                                record_timeout: Duration::from_secs(3),
                            },
                            TlsCancellation::default(),
                        )
                        .map_err(io::Error::from)
                },
            )?;
            ready_send.send(Some(port)).map_err(|_| invalid())?;
            frames.serve(&mut server, &stop, || epoch.elapsed().as_millis() as u64)
        },
    )?;
    if ready.recv_timeout(Duration::from_secs(10))?.is_some() {
        return Err(invalid().into());
    }
    let (command_send, command) = mpsc::sync_channel(1);
    let input = thread::spawn(move || controls(command_send));
    let mut out = io::stdout().lock();
    writeln!(out, "{{\"state\":\"csr-ready\"}}")?;
    out.flush()?;
    if command.recv_timeout(Duration::from_secs(15))? != "enrolled" {
        return Err(invalid().into());
    }
    enroll_send.send(())?;
    let port = ready
        .recv_timeout(Duration::from_secs(10))?
        .ok_or_else(invalid)?;
    let paused = Arc::new(AtomicBool::new(false));
    let mut app = ThreadedCamera::<_, 153600, _, _>::new(
        Images {
            images,
            format: None,
            paused: paused.clone(),
            next: 0,
            index: 0,
        },
        vec![0; 153600],
        vec![0; 153600],
        worker,
    )
    .map_err(|_| invalid())?;
    app.start(320, 240).map_err(|_| invalid())?;
    writeln!(
        out,
        "{{\"port\":{port},\"implementation\":\"rust-sdk-tls-stream\"}}"
    )?;
    out.flush()?;
    let mut peak = 0;
    loop {
        match command.try_recv() {
            Ok(command) => {
                let quit = command == "quit" || command == "eof";
                match command.as_str() {
                    "pause" => paused.store(true, Ordering::Release),
                    "resume" => paused.store(false, Ordering::Release),
                    "status" => {}
                    "quit" | "eof" => {
                        app.stop().map_err(|_| invalid())?;
                        app.finish_worker().map_err(|_| invalid())?;
                    }
                    _ => return Err(invalid().into()),
                }
                let stats = app.stats().capture.stream;
                let active = stats
                    .accepted
                    .checked_sub(stats.disconnected)
                    .ok_or_else(invalid)?;
                if active > 1 {
                    return Err(invalid().into());
                }
                peak = peak.max(active);
                writeln!(out,"{{\"active\":{active},\"peak\":{peak},\"connections\":{},\"disconnected\":{},\"frames\":{},\"busy\":{}}}",stats.accepted,stats.disconnected,stats.frames_completed,stats.rejected_busy)?;
                out.flush()?;
                if quit {
                    break;
                }
            }
            Err(mpsc::TryRecvError::Disconnected) => {
                app.stop().map_err(|_| invalid())?;
                app.finish_worker().map_err(|_| invalid())?;
                break;
            }
            Err(mpsc::TryRecvError::Empty) => {}
        }
        app.poll(epoch.elapsed().as_millis() as u64, true)
            .map_err(|_| invalid())?;
        let stats = app.stats().capture.stream;
        let outstanding = stats
            .accepted
            .checked_sub(stats.disconnected)
            .ok_or_else(invalid)?;
        if outstanding > 1 {
            return Err(invalid().into());
        }
        peak = peak.max(outstanding);
        thread::sleep(Duration::from_millis(5));
    }
    input.join().map_err(|_| invalid())??;
    Ok(())
}
fn main() {
    if run().is_err() {
        eprintln!("SDK camera fixture failed; private diagnostics retained by caller");
        std::process::exit(1);
    }
}
