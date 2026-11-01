//! SDK interoperability fixture. Host PKI/time observations are test inputs;
//! this is not a provisioning service or a physical-clock qualification.
#![forbid(unsafe_code)]
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
mod bootstrap;
mod product;

#[derive(Clone, Copy)]
enum Case {
    Echo,
    Bulk,
    ChangedRetry,
    Refresh,
    Rollback,
    ChangedEnrollment,
    Http,
    Product,
    Media,
    Interleave,
    Network,
    NetworkTimeStall,
}

fn public(path: PathBuf) -> Vec<u8> {
    let mut bytes = Vec::new();
    std::fs::File::open(path)
        .unwrap()
        .take(16_385)
        .read_to_end(&mut bytes)
        .unwrap();
    assert!(bytes.len() <= 16_384);
    bytes
}
fn policy(directory: &Path) -> tls::CertificatePolicy {
    tls::CertificatePolicy {
        issuer_sha256: public(directory.join("issuer.sha256")).try_into().unwrap(),
        own_uri: String::from_utf8(public(directory.join("own.uri"))).unwrap(),
        peer_uri: String::from_utf8(public(directory.join("peer.uri"))).unwrap(),
    }
}
fn transaction(
    identity: &mut tls::Identity,
    socket: std::net::TcpStream,
    certs: tls::Certificates<'_>,
    age: u64,
    case: Case,
) -> Result<(), tls::Error> {
    let epoch = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let time = tls::TimeLease::from_trusted(epoch, Duration::from_millis(age))?;
    identity.configure(
        tls::Certificates {
            policy: certs.policy,
            own: certs.own,
            issuer: certs.issuer,
            approved_peer: certs.approved_peer,
            revocations: certs.revocations,
        },
        &time,
    )?;
    if matches!(case, Case::ChangedEnrollment) {
        let rejected = tls::CertificatePolicy {
            issuer_sha256: [0; 32],
            own_uri: certs.policy.own_uri.clone(),
            peer_uri: certs.policy.peer_uri.clone(),
        };
        assert_eq!(
            identity.configure(
                tls::Certificates {
                    policy: &rejected,
                    ..certs
                },
                &time,
            ),
            Err(tls::Error::Authentication),
        );
        let result = identity.accept(socket, time);
        assert!(matches!(result, Err(tls::Error::InvalidInput)));
        return result.map(|_| ());
    }
    let mut connection = identity.accept(socket, time)?;
    let deadline = Instant::now() + Duration::from_secs(10);
    if matches!(case, Case::Http) {
        while !connection.poll()? {
            if Instant::now() >= deadline {
                return Err(tls::Error::Time);
            }
            std::thread::sleep(Duration::from_millis(1));
        }
        let start = Instant::now();
        let mut http =
            stream_service::HttpSession::new(vec![0; 16_384], stream_service::Options::default())
                .map_err(|_| tls::Error::Capacity)?;
        assert!(http.accept(connection, 0));
        let mut offered = None;
        while http.stats().frames_completed < 2 {
            if Instant::now() >= deadline {
                return Err(tls::Error::Time);
            }
            let now = start.elapsed().as_millis() as u64;
            let sequence = http.stats().frames_completed + 1;
            let bytes: &[u8] = if sequence == 1 {
                include_bytes!("../nuttx-std/detection/background.jpg")
            } else {
                include_bytes!("../nuttx-std/detection/person.jpg")
            };
            let frame = camera::Frame {
                sequence,
                bytes,
                capture: camera::Capture {
                    format: camera::Format {
                        width: 96,
                        height: 96,
                        pixels: camera::PixelFormat::Jpeg,
                    },
                    timestamp_ms: now,
                    len: bytes.len(),
                },
            };
            let progress = http.poll(now, Some(frame));
            if progress.frame_accepted {
                offered = Some((sequence, now));
            }
            if progress.frames_completed != 0 {
                let receipt = http.stats().latest_completed.unwrap();
                assert_eq!(Some((receipt.sequence, receipt.captured_ms)), offered);
                assert_eq!(receipt.completed_ms, now);
            }
            if !progress.active {
                return Err(tls::Error::Io);
            }
            std::thread::sleep(Duration::from_millis(1));
        }
        assert_eq!(http.stats().errors, 0);
        assert_eq!(http.into_buffer().len(), 16_384);
        return Ok(());
    }
    for turn in 0..if matches!(case, Case::Refresh) { 2 } else { 1 } {
        let mut input = [0; 5];
        let mut read = 0;
        while read != input.len() {
            if Instant::now() >= deadline {
                return Err(tls::Error::Time);
            }
            match connection.read(&mut input[read..]) {
                Ok(0) => return Err(tls::Error::Io),
                Ok(n) => read += n,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(1))
                }
                Err(_) => return Err(tls::Error::Authentication),
            }
        }
        if &input != b"ping\n" {
            return Err(tls::Error::InvalidInput);
        }
        if matches!(case, Case::Interleave) {
            let directory = PathBuf::from(std::env::args_os().nth(1).unwrap());
            bootstrap::exchange(identity, &directory, 3000)?;
        }
        if matches!(case, Case::Rollback) {
            let rollback = tls::TimeLease::from_trusted(epoch - 10, Duration::from_secs(5))?;
            assert_eq!(connection.refresh_time(rollback), Err(tls::Error::Time));
            assert_eq!(connection.poll(), Err(tls::Error::Poisoned));
            return Err(tls::Error::Time);
        }
        if matches!(case, Case::Refresh) && turn == 0 {
            let observed = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_secs();
            connection.refresh_time(tls::TimeLease::from_trusted(
                observed,
                Duration::from_secs(5),
            )?)?;
            println!("REFRESHED");
            std::io::stdout().flush().unwrap();
        }
    }
    if matches!(case, Case::Bulk | Case::ChangedRetry) {
        let chunk = [0x5a; 4096];
        let mut written = 0;
        let mut blocked = false;
        while written < 16 * 1024 * 1024 {
            if Instant::now() >= deadline {
                return Err(tls::Error::Time);
            }
            let remaining = (16 * 1024 * 1024 - written).min(chunk.len());
            match connection.write(&chunk[..remaining]) {
                Ok(n) if n != 0 => written += n,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    if !blocked {
                        println!("BLOCKED");
                        std::io::stdout().flush().unwrap();
                        blocked = true;
                    }
                    if matches!(case, Case::ChangedRetry) {
                        assert_eq!(
                            connection.write(b"changed").unwrap_err().kind(),
                            std::io::ErrorKind::InvalidInput
                        );
                        let mut refused = [0xa5; 16];
                        assert_eq!(
                            connection.read(&mut refused).unwrap_err().kind(),
                            std::io::ErrorKind::NotConnected
                        );
                        assert!(refused.iter().all(|b| *b == 0));
                        return Err(tls::Error::InvalidInput);
                    }
                    std::thread::sleep(Duration::from_millis(1));
                }
                _ => return Err(tls::Error::Io),
            }
        }
        assert!(
            blocked,
            "independent slow reader must exercise SDK write retry"
        );
    }
    let mut written = 0;
    while written != 5 {
        if Instant::now() >= deadline {
            return Err(tls::Error::Time);
        }
        match connection.write(&b"pong\n"[written..]) {
            Ok(n) if n != 0 => written += n,
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                std::thread::sleep(Duration::from_millis(1))
            }
            _ => return Err(tls::Error::Io),
        }
    }
    while !connection.close()? {
        if Instant::now() >= deadline {
            return Err(tls::Error::Time);
        }
        std::thread::sleep(Duration::from_millis(1));
    }
    Ok(())
}
fn main() {
    let directory = PathBuf::from(
        std::env::args_os()
            .nth(1)
            .expect("public credential directory"),
    );
    let age: u64 = std::env::args()
        .nth(2)
        .unwrap_or_else(|| "30000".into())
        .parse()
        .unwrap();
    if std::env::args().nth(3).as_deref() == Some("bootstrap") {
        bootstrap::run(directory, age);
        return;
    }
    let case = match std::env::args().nth(3).as_deref() {
        None | Some("echo") => Case::Echo,
        Some("bulk") => Case::Bulk,
        Some("changed-retry") => Case::ChangedRetry,
        Some("refresh") => Case::Refresh,
        Some("rollback") => Case::Rollback,
        Some("changed-enrollment") => Case::ChangedEnrollment,
        Some("http") => Case::Http,
        Some("product") => Case::Product,
        Some("media") => Case::Media,
        Some("interleave") => Case::Interleave,
        Some("network") => Case::Network,
        Some("network-time-stall") => Case::NetworkTimeStall,
        _ => panic!("unknown fixture case"),
    };
    if matches!(
        case,
        Case::Product | Case::Media | Case::Network | Case::NetworkTimeStall
    ) {
        let stack = std::env::args()
            .nth(4)
            .map_or(65536, |v| v.parse().unwrap());
        product::run(directory, age, stack, case);
        return;
    }
    let mut identity = tls::open().expect("SDK identity");
    let mut csr = [0; 1024];
    let size = identity.request("fixture", &mut csr).unwrap();
    std::fs::write(directory.join("request.csr"), &csr[..size]).unwrap();
    println!("CSR_READY");
    std::io::stdout().flush().unwrap();
    let mut line = String::new();
    std::io::stdin().read_line(&mut line).unwrap();
    assert_eq!(line.trim(), "load");
    let own = public(directory.join("server.der"));
    let policy = policy(&directory);
    let issuer = public(directory.join("issuer.der"));
    let peer = public(directory.join("peer.der"));
    let revocations = public(directory.join("revocations.der"));
    let address = std::net::SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, 0);
    let listener = std::net::TcpListener::bind(address).unwrap();
    listener.set_nonblocking(true).unwrap();
    println!("LISTEN {}", listener.local_addr().unwrap());
    std::io::stdout().flush().unwrap();
    let deadline = Instant::now() + Duration::from_secs(20);
    while Instant::now() < deadline {
        match listener.accept() {
            Ok((socket, _)) => {
                let certs = tls::Certificates {
                    policy: &policy,
                    own: &own,
                    issuer: &issuer,
                    approved_peer: &peer,
                    revocations: &revocations,
                };
                let result = transaction(&mut identity, socket, certs, age, case);
                // Closing a connection must not destroy its enrolled key.
                let count = identity
                    .request("fixture", &mut csr)
                    .expect("key survives connection retirement");
                std::fs::write(directory.join("retired.csr"), &csr[..count]).unwrap();
                println!("RESULT {result:?}");
                std::io::stdout().flush().unwrap();
                return;
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                std::thread::sleep(Duration::from_millis(1))
            }
            Err(error) => panic!("fixture accept: {error}"),
        }
    }
    panic!(
        "fixture client deadline; directory={}",
        Path::new(&directory).display()
    );
}
