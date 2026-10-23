//! Actual target TLS/loopback prototype. Console is only a private ciphertext
//! relay to an independent host TLS peer. No production entropy/time claim.
#![deny(unsafe_code)]
#[cfg_attr(not(target_os = "nuttx"), allow(dead_code))]
mod resources;
#[cfg_attr(not(target_os = "nuttx"), allow(dead_code))]
mod witness {
    #![forbid(unsafe_code)]
    use crate::resources::Sample;
    use camera::{Camera, Capture, DeviceError, Format};
    use camera_stream::ThreadedCamera;
    use device_identity::{
        CameraCertificatePolicy, Entropy, EntropyUnavailable, Error, Identity, RevocationState,
        TlsCancellation, TlsError, TlsPolicy,
    };
    use std::{
        cell::RefCell,
        io::{self, BufRead, Read, Write},
        net::{SocketAddr, TcpListener, TcpStream},
        rc::Rc,
        sync::mpsc,
        thread,
        time::{Duration, Instant},
    };
    use stream_service::{Options, Server, StreamWorker, WorkerOptions};
    const MAX_FRAME: usize = 2048;
    const RELAY_BYTES: usize = 4096;
    struct Pool {
        bytes: Vec<u8>,
        position: usize,
    }
    struct Source(Rc<RefCell<Pool>>);
    impl Entropy for Source {
        fn fill(&mut self, output: &mut [u8]) -> Result<(), EntropyUnavailable> {
            let mut p = self.0.try_borrow_mut().map_err(|_| EntropyUnavailable)?;
            let end = p
                .position
                .checked_add(output.len())
                .ok_or(EntropyUnavailable)?;
            if end > p.bytes.len() {
                return Err(EntropyUnavailable);
            }
            let start = p.position;
            output.copy_from_slice(&p.bytes[start..end]);
            p.bytes[start..end].fill(0);
            p.position = end;
            Ok(())
        }
    }
    struct Image {
        format: Option<Format>,
        next: u64,
    }
    impl Camera for Image {
        fn start(&mut self, format: Format) -> Result<Format, DeviceError> {
            self.format = Some(format);
            Ok(format)
        }
        fn poll_frame(&mut self, now: u64, out: &mut [u8]) -> Result<Option<Capture>, DeviceError> {
            if now < self.next {
                return Ok(None);
            }
            let image = include_bytes!("../../../device-identity/frame.jpg");
            out[..image.len()].copy_from_slice(image);
            self.next = now.saturating_add(20);
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
    fn line(input: &mut impl BufRead) -> String {
        let mut value = String::new();
        let n = (&mut *input)
            .take(130)
            .read_line(&mut value)
            .expect("relay line");
        assert!(n > 0 && n < 130 && value.ends_with('\n'));
        value.trim_end_matches(['\r', '\n']).to_owned()
    }
    fn decimal(value: &str, max: usize) -> usize {
        let n: usize = value.parse().expect("relay length");
        assert!(n > 0 && n <= max && value == n.to_string());
        n
    }
    fn payload(input: &mut impl BufRead, n: usize) -> Vec<u8> {
        let mut output = Vec::with_capacity(n);
        while output.len() < n {
            let text = line(input);
            assert!(!text.is_empty() && text.len() <= 64 && text.len().is_multiple_of(2));
            assert!(
                output.len() + text.len() / 2 <= n && text.bytes().all(|b| b.is_ascii_hexdigit())
            );
            for i in (0..text.len()).step_by(2) {
                output.push(u8::from_str_radix(&text[i..i + 2], 16).expect("relay hex"));
            }
        }
        output
    }
    #[cfg(test)]
    #[test]
    fn console_payload_and_length_bounds_refuse_ambiguous_or_oversized_frames() {
        assert_eq!(decimal("4096", 4096), 4096);
        for value in ["0", "4097", "+1", "01", " 1", "1 "] {
            assert!(std::panic::catch_unwind(|| decimal(value, 4096)).is_err());
        }
        assert_eq!(
            payload(&mut io::Cursor::new(b"00a1ff\n"), 3),
            vec![0, 0xa1, 0xff]
        );
        for value in ["00a1ff00\n", "00a1f\n", "zz\n", "", "\n"] {
            assert!(std::panic::catch_unwind(|| payload(
                &mut io::Cursor::new(value.as_bytes()),
                3
            ))
            .is_err());
        }
    }
    enum Command {
        Enroll(Vec<u8>, Vec<u8>),
        Send(usize, Vec<u8>),
        Read(usize),
        Reconnect,
        Quit,
    }
    fn commands(sender: mpsc::SyncSender<Command>) {
        let mut input = io::stdin().lock();
        loop {
            let value = line(&mut input);
            let command = if value == "ENROLL" {
                let header = line(&mut input);
                let n = decimal(header.strip_prefix("DER ").expect("leaf header"), 2048);
                let leaf = payload(&mut input, n);
                let header = line(&mut input);
                let n = decimal(header.strip_prefix("DER ").expect("CRL header"), 4096);
                Command::Enroll(leaf, payload(&mut input, n))
            } else if let Some(fields) = value.strip_prefix("SEND ") {
                let (id, length) = fields.split_once(' ').expect("send fields");
                let id = decimal(id, 100000);
                let n = decimal(length, RELAY_BYTES);
                Command::Send(id, payload(&mut input, n))
            } else if let Some(id) = value.strip_prefix("READ ") {
                Command::Read(decimal(id, 100000))
            } else if value == "RECONNECT" {
                Command::Reconnect
            } else if value == "QUIT" {
                Command::Quit
            } else {
                panic!("unknown relay command")
            };
            let quit = matches!(command, Command::Quit);
            if sender.send(command).is_err() || quit {
                return;
            }
        }
    }
    fn frame(output: &mut impl Write, id: usize, bytes: &[u8], pending: usize, eof: bool) {
        writeln!(
            output,
            "NXRS_TLS_FRAME {id} {} pending={pending} eof={}",
            bytes.len(),
            usize::from(eof)
        )
        .unwrap();
        for chunk in bytes.chunks(32) {
            for b in chunk {
                write!(output, "{b:02x}").unwrap();
            }
            writeln!(output).unwrap();
        }
        writeln!(output, "NXRS_TLS_FRAME_END {id}").unwrap();
        output.flush().unwrap();
    }
    pub fn run(seed: &[u8], root: &'static [u8], pin: &'static [u8; 32], clock: &'static [u8; 8]) {
        println!("NXRS_TLS_TARGET_ENTERED");
        let main_before = Sample::read();
        assert_eq!(seed.len(), 4096);
        let entropy = seed.to_vec();
        let now = u64::from_le_bytes(*clock);
        let epoch = Instant::now();
        let (ready_send, ready) = mpsc::sync_channel(1);
        let (enroll_send, enroll) = mpsc::sync_channel::<(Vec<u8>, Vec<u8>)>(1);
        let (metrics_send, metrics) = mpsc::sync_channel(1);
        let worker = StreamWorker::spawn(
            vec![0; MAX_FRAME],
            WorkerOptions {
                stack_bytes: 65536,
                poll_interval: Duration::from_millis(2),
            },
            move |mut frames, stop| {
                let worker_before = Sample::read();
                let map = |e| io::Error::from(TlsError::from(e));
                let pool = Rc::new(RefCell::new(Pool {
                    bytes: entropy,
                    position: 0,
                }));
                let mut owner = Identity::generate(Source(pool.clone())).map_err(map)?;
                let mut csr = [0; 2048];
                let n = owner.request("cam_a", &mut csr).map_err(map)?;
                let listener = TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], 0)))?;
                let port = listener.local_addr()?.port();
                ready_send
                    .send((csr[..n].to_vec(), port))
                    .map_err(|_| io::ErrorKind::BrokenPipe)?;
                csr.fill(0);
                let (leaf, crl) = enroll
                    .recv_timeout(Duration::from_secs(30))
                    .map_err(|_| io::ErrorKind::TimedOut)?;
                let state = RevocationState::from_trusted_floor(*pin, 1).map_err(map)?;
                let policy = || CameraCertificatePolicy {
                    issuer_sha256: *pin,
                    site_id: "site_a",
                    camera_id: "cam_a",
                    server_ip: [127, 0, 0, 1],
                    trusted_unix_seconds: now + epoch.elapsed().as_secs(),
                };
                let mut server = Server::with_transport(
                    listener,
                    vec![0; MAX_FRAME],
                    Options {
                        request_timeout_ms: 30000,
                        send_deadline_ms: 5000,
                        max_frame_age_ms: Some(1000),
                        idle_timeout_ms: Some(10000),
                    },
                    |socket| {
                        owner
                            .bind_current_certificate(&leaf, root, policy(), &state, &crl)
                            .map_err(map)?
                            .tls_server_tcp(
                                socket,
                                root,
                                TlsPolicy {
                                    camera: policy(),
                                    gateway_id: "gw_a",
                                    handshake_timeout: Duration::from_secs(30),
                                    record_timeout: Duration::from_secs(5),
                                },
                                TlsCancellation::default(),
                            )
                            .map_err(io::Error::from)
                    },
                )?;
                let result =
                    frames.serve(&mut server, &stop, || epoch.elapsed().as_millis() as u64);
                drop(server);
                drop(owner);
                pool.borrow_mut().bytes.fill(0);
                // Force the current scheduler slice to be charged before the
                // accounting observation; this is not cycle-accurate CPU time.
                thread::sleep(Duration::from_millis(1));
                metrics_send.send((worker_before, Sample::read())).unwrap();
                result
            },
        )
        .expect("target TLS worker");
        let mut app = ThreadedCamera::<_, MAX_FRAME, _, _>::new(
            Image {
                format: None,
                next: 0,
            },
            vec![0; MAX_FRAME],
            vec![0; MAX_FRAME],
            worker,
        )
        .expect("target camera composition");
        app.start(320, 240).expect("target synthetic capture");
        let (command_send, command) = mpsc::sync_channel(1);
        let input = thread::Builder::new()
            .stack_size(16384)
            .spawn(move || {
                let before = Sample::read();
                commands(command_send);
                thread::sleep(Duration::from_millis(1));
                (before, Sample::read())
            })
            .expect("relay input thread");
        let mut output = io::stdout().lock();
        let mut bridge: Option<TcpStream> = None;
        let mut address = None;
        let mut rejected = 0;
        let mut reconnect = false;
        let mut enrolled = false;
        let mut pending = Vec::new();
        let mut offset = 0;
        let mut send_started: Option<Instant> = None;
        let mut rx = 0usize;
        let mut tx = 0usize;
        let mut write_closed = false;
        let mut closed_writes = 0;
        let mut last = 0;
        let mut gap = 0;
        loop {
            let tick = epoch.elapsed().as_millis() as u64;
            gap = gap.max(tick.saturating_sub(last));
            last = tick;
            app.poll(tick, true).expect("target capture step");
            if bridge.is_none() {
                if let Ok((csr, port)) = ready.try_recv() {
                    writeln!(output, "NXRS_TLS_CSR_BEGIN").unwrap();
                    output.write_all(&csr).unwrap();
                    writeln!(output, "NXRS_TLS_CSR_END").unwrap();
                    output.flush().unwrap();
                    let peer = SocketAddr::from(([127, 0, 0, 1], port));
                    address = Some(peer);
                    let socket = TcpStream::connect_timeout(&peer, Duration::from_secs(3))
                        .expect("native TCP loopback connect");
                    socket.set_nonblocking(true).unwrap();
                    socket.set_nodelay(true).unwrap();
                    bridge = Some(socket);
                }
            }
            if let Some(socket) = bridge.as_mut() {
                for _ in 0..8 {
                    if write_closed {
                        pending.clear();
                        offset = 0;
                    }
                    if offset == pending.len() {
                        break;
                    }
                    match socket.write(&pending[offset..]) {
                        Ok(n) if n > 0 => {
                            offset += n;
                            tx += n;
                        }
                        Err(e)
                            if matches!(
                                e.kind(),
                                io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                            ) =>
                        {
                            break
                        }
                        Ok(_) | Err(_) => {
                            // SEND_ACCEPTED acknowledges bounded relay admission.
                            // A rejecting TLS server may close before its client's
                            // remaining ciphertext is written. Retain the actual
                            // byte count, drain EOF through READ and require the
                            // independent server rejection counters before reuse.
                            write_closed = true;
                            closed_writes += 1;
                            pending.clear();
                            offset = 0;
                            break;
                        }
                    }
                }
                if offset == pending.len() {
                    pending.clear();
                    offset = 0;
                    send_started = None;
                }
            }
            if let Some(start) = send_started {
                assert!(
                    start.elapsed() < Duration::from_secs(5),
                    "relay send deadline"
                );
            }
            match command.try_recv() {
                Ok(Command::Enroll(leaf, crl)) => {
                    assert!(!enrolled && bridge.is_some());
                    enroll_send.send((leaf, crl)).unwrap();
                    enrolled = true;
                    writeln!(output, "NXRS_TLS_ENROLLED").unwrap();
                    output.flush().unwrap();
                }
                Ok(Command::Send(id, bytes)) => {
                    assert!(enrolled && pending.is_empty());
                    pending = bytes;
                    offset = 0;
                    send_started = Some(Instant::now());
                    writeln!(output, "NXRS_TLS_SEND_ACCEPTED {id}").unwrap();
                    output.flush().unwrap();
                }
                Ok(Command::Read(id)) => {
                    let socket = bridge.as_mut().expect("relay connection");
                    let mut bytes = [0; 512];
                    let (n, eof) = match socket.read(&mut bytes) {
                        Ok(n) => (n, n == 0),
                        Err(e)
                            if matches!(
                                e.kind(),
                                io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                            ) =>
                        {
                            (0, false)
                        }
                        Err(_) => (0, true),
                    };
                    rx += n;
                    frame(&mut output, id, &bytes[..n], pending.len() - offset, eof);
                }
                Ok(Command::Reconnect) => {
                    assert!(enrolled && !reconnect && rejected < 2 && pending.is_empty());
                    reconnect = true;
                }
                Ok(Command::Quit) => break,
                Err(mpsc::TryRecvError::Disconnected) => panic!("relay input retired early"),
                Err(mpsc::TryRecvError::Empty) => {}
            }
            if reconnect {
                let stats = app.stats().capture.stream;
                if stats.disconnected == rejected + 1 {
                    assert!(stats.accepted == rejected + 1 && stats.errors == rejected + 1);
                    assert_eq!(stats.frames_completed, 0, "rejected peers received media");
                    drop(bridge.take());
                    let socket = TcpStream::connect_timeout(
                        &address.expect("ready listener"),
                        Duration::from_secs(3),
                    )
                    .expect("native TCP reconnect after refusal");
                    socket.set_nonblocking(true).unwrap();
                    socket.set_nodelay(true).unwrap();
                    bridge = Some(socket);
                    write_closed = false;
                    rejected += 1;
                    reconnect = false;
                    writeln!(output, "NXRS_TLS_RECONNECTED {rejected} refused_frames=0").unwrap();
                    output.flush().unwrap();
                }
            }
            assert!(
                epoch.elapsed() < Duration::from_secs(150),
                "target TLS overall deadline"
            );
            thread::sleep(Duration::from_millis(1));
        }
        app.stop().expect("target capture stop");
        app.finish_worker().expect("target SDK native retirement");
        let (reader_before, reader_after) = input.join().expect("relay reader join");
        let (worker_before, worker_after) = metrics.recv().expect("joined worker metrics");
        struct Unavailable;
        impl Entropy for Unavailable {
            fn fill(&mut self, _: &mut [u8]) -> Result<(), EntropyUnavailable> {
                Err(EntropyUnavailable)
            }
        }
        assert!(
            matches!(Identity::generate(Unavailable), Err(Error::Entropy)),
            "SDK lease must retire before fresh entropy admission"
        );
        let stats = app.stats();
        assert!(
            stats.capture.stream.frames_completed >= 2
                && stats.capture.stream.accepted == 3
                && stats.capture.stream.disconnected == 3
                && stats.capture.stream.errors == 2
                && rejected == 2
        );
        writeln!(output,"NXRS_TLS_TARGET_REPORT captured={} busy={} frames={} tx={tx} rx={rx} gap_ms={gap} elapsed_ms={} buffer_bytes={} worker_stack=65536 closed_writes={closed_writes} rejected=2 refused_frames=0 accepted=3 disconnected=3 errors=2 retired=1 lease_released=1",stats.capture.captured,stats.skipped_busy,stats.capture.stream.frames_completed,epoch.elapsed().as_millis(),4*MAX_FRAME).unwrap();
        output.flush().unwrap();
        drop(output);
        drop(bridge.take());
        drop(pending);
        drop(app);
        thread::sleep(Duration::from_millis(1));
        let main_after = Sample::read();
        main_after.report("capture", main_before);
        worker_after.report("worker", worker_before);
        reader_after.report("relay", reader_before);
    }
}
fn main() {
    #[cfg(target_os = "nuttx")]
    witness::run(
        include_bytes!(env!("NXRS_IDENTITY_ENTROPY_FIXTURE")),
        include_bytes!(concat!(
            env!("NXRS_IDENTITY_CERTIFICATE_FIXTURE"),
            "/root.der"
        )),
        include_bytes!(concat!(
            env!("NXRS_IDENTITY_CERTIFICATE_FIXTURE"),
            "/pin.bin"
        )),
        include_bytes!(concat!(
            env!("NXRS_IDENTITY_CERTIFICATE_FIXTURE"),
            "/time.bin"
        )),
    );
    #[cfg(not(target_os = "nuttx"))]
    panic!("target TLS witness must execute inside NuttX");
}
