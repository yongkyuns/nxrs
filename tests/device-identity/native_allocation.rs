//! Test-only SDK allocation-site failures; Rust owns all injection and accounting.
use super::*;
use std::collections::BTreeSet;
use std::io::Write;

type Site = (&'static [u8], u32);
#[derive(Clone, Copy)]
enum Mode {
    Off,
    Record,
    Fail(Site),
}
struct Trace {
    mode: Mode,
    sites: BTreeSet<Site>,
    armed: bool,
    hit: bool,
    refused: usize,
    calls: usize,
    overflow: bool,
}
impl Trace {
    const fn new() -> Self {
        Self {
            mode: Mode::Off,
            sites: BTreeSet::new(),
            armed: false,
            hit: false,
            refused: 0,
            calls: 0,
            overflow: false,
        }
    }
}
thread_local! {
    static TRACE: RefCell<Trace> = const { RefCell::new(Trace::new()) };
    static LIVE: Cell<isize> = const { Cell::new(0) };
    static BAD_ACCOUNTING: Cell<bool> = const { Cell::new(false) };
}
unsafe extern "C" {
    fn calloc(count: usize, size: usize) -> *mut c_void;
    fn free(pointer: *mut c_void);
}
#[unsafe(no_mangle)]
unsafe extern "C" fn nxrs_test_di_calloc_at(
    count: usize,
    size: usize,
    file: *const c_char,
    line: u32,
) -> *mut c_void {
    if count != 0 && size != 0 {
        // SAFETY: the test SDK macro supplies the compiler's static NUL-terminated
        // __FILE__ string. This export is linked only into this SDK witness.
        let file: &'static [u8] = unsafe { std::ffi::CStr::from_ptr(file) }.to_bytes();
        let deny = TRACE.with(|state| {
            let mut trace = state.borrow_mut();
            match trace.mode {
                Mode::Off => false,
                Mode::Record => {
                    trace.calls += 1;
                    if trace.sites.len() < 256 || trace.sites.contains(&(file, line)) {
                        trace.sites.insert((file, line));
                    } else {
                        trace.overflow = true;
                    }
                    false
                }
                Mode::Fail(target) => {
                    trace.calls += 1;
                    if (file, line) == target {
                        trace.hit = true;
                        trace.armed = true;
                    }
                    if trace.armed {
                        trace.refused += 1;
                        true
                    } else {
                        false
                    }
                }
            }
        });
        if deny {
            return ptr::null_mut();
        }
    }
    // SAFETY: exact host allocator ABI and zero-initialization contract.
    let pointer = unsafe { calloc(count, size) };
    if !pointer.is_null() {
        LIVE.with(|value| value.set(value.get() + 1));
    }
    pointer
}
#[unsafe(no_mangle)]
unsafe extern "C" fn nxrs_test_di_free(pointer: *mut c_void) {
    if !pointer.is_null() {
        LIVE.with(|value| {
            if value.get() <= 0 {
                BAD_ACCOUNTING.with(|bad| bad.set(true));
            }
            value.set(value.get() - 1);
        });
    }
    // SAFETY: native SDK frees only allocations from its paired callback.
    unsafe { free(pointer) }
}
struct Reset;
impl Drop for Reset {
    fn drop(&mut self) {
        TRACE.with(|state| state.borrow_mut().mode = Mode::Off);
    }
}
fn invoke<T>(target: Option<Site>, operation: impl FnOnce() -> T) -> (T, Trace) {
    TRACE.with(|state| {
        let mut state = state.borrow_mut();
        assert!(matches!(state.mode, Mode::Off));
        *state = Trace::new();
        state.mode = target.map(Mode::Fail).unwrap_or(Mode::Record);
    });
    let reset = Reset;
    let result = operation();
    drop(reset);
    let trace = TRACE.with(|state| std::mem::replace(&mut *state.borrow_mut(), Trace::new()));
    assert!(!trace.overflow);
    assert!(!BAD_ACCOUNTING.with(Cell::get));
    (result, trace)
}
fn balanced() {
    assert_eq!(LIVE.with(Cell::get), 0);
    assert!(!BAD_ACCOUNTING.with(Cell::get));
}
struct Observation {
    trace: Trace,
    failed: bool,
}
fn sweep(name: &str, mut attempt: impl FnMut(Option<Site>) -> Observation) {
    let baseline = attempt(None);
    balanced();
    assert!(!baseline.failed && baseline.trace.refused == 0);
    let calls = baseline.trace.calls;
    let sites = baseline.trace.sites;
    assert!(
        !sites.is_empty() || matches!(name, "native-tls-read.counts" | "native-tls-write.counts")
    );
    let mut failed = 0;
    let mut continued = 0;
    let mut attempts = 0;
    for site in &sites {
        let mut reached = false;
        // Some bignum allocation paths depend on a generated scalar. Every
        // unexercised attempt must still succeed, and every selected site must
        // actually be refused within this finite bound; no fault is discarded.
        for _ in 0..8 {
            attempts += 1;
            let result = attempt(Some(*site));
            balanced();
            if !result.trace.hit {
                assert!(!result.failed && result.trace.refused == 0);
                continue;
            }
            assert!(result.trace.refused > 0);
            reached = true;
            if result.failed {
                failed += 1;
            } else {
                continued += 1;
            }
            break;
        }
        assert!(
            reached,
            "selected native allocation site was not exercised: {site:?}"
        );
        // Require a fresh successful operation after every actual refusal.
        // A positive baseline before faults cannot prove recovery afterward.
        let recovery = attempt(None);
        balanced();
        assert!(!recovery.failed && recovery.trace.refused == 0);
    }
    assert_eq!(failed + continued, sites.len());
    let root =
        std::path::PathBuf::from(std::env::var_os("NXRS_IDENTITY_CERTIFICATE_FIXTURE").unwrap());
    std::fs::write(
        root.join(name),
        format!(
            "{calls} {} {failed} {continued} {attempts} {}\n",
            sites.len(),
            sites.len()
        ),
    )
    .unwrap();
}
fn response<T: std::io::Read + std::io::Write>(
    session: &mut TlsServerSession<'_, '_, '_, T>,
    client: &mut TlsClient,
) {
    let response = b"nxrs actual SDK mutual TLS response\n";
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(3);
    loop {
        match session.write(response) {
            Ok(length) => {
                assert_eq!(length, response.len());
                break;
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {}
            other => panic!("native allocation recovery response: {other:?}"),
        }
        assert!(std::time::Instant::now() < deadline);
        std::thread::sleep(std::time::Duration::from_millis(1));
    }
    wait_client(client, true);
}
#[test]
fn native_allocator_refusal_and_pairing_are_real() {
    let _serial = serial();
    balanced();
    let probe = c"native-allocator-probe";
    let (pointer, trace) = invoke(None, || unsafe {
        nxrs_test_di_calloc_at(2, 16, probe.as_ptr(), 1)
    });
    assert_eq!(trace.calls, 1);
    assert_eq!(trace.sites.len(), 1);
    assert!(!pointer.is_null());
    assert!(
        unsafe { std::slice::from_raw_parts(pointer.cast::<u8>(), 32) }
            .iter()
            .all(|b| *b == 0)
    );
    unsafe { nxrs_test_di_free(pointer) };
    balanced();
    let site = *trace.sites.first().unwrap();
    let (pointer, trace) = invoke(Some(site), || unsafe {
        nxrs_test_di_calloc_at(1, 16, probe.as_ptr(), 1)
    });
    assert!(pointer.is_null() && trace.hit && trace.refused == 1);
    balanced();
}
#[test]
fn native_identity_and_csr_refusals_retire_sources_and_recover() {
    let _serial = serial();
    balanced();
    sweep("native-identity.counts", |target| {
        let dropped = Arc::new(AtomicBool::new(false));
        let source = AllocationSource {
            os: OsEntropy::open(),
            dropped: dropped.clone(),
        };
        let (result, trace) = invoke(target, || Identity::generate(source));
        let failed = result.is_err();
        drop(result);
        assert!(dropped.load(Ordering::Acquire));
        assert!(!OWNED.load(Ordering::Acquire));
        Observation { trace, failed }
    });
    sweep("native-csr.counts", |target| {
        let mut identity = Identity::generate(OsEntropy::open()).unwrap();
        let mut output = [0xa5; 2048];
        let (result, trace) = invoke(target, || identity.request("cam_A", &mut output));
        let failed = result.is_err();
        match result {
            Err(_) => assert!(output.iter().all(|b| *b == 0)),
            Ok(length) => {
                let path = std::path::PathBuf::from(
                    std::env::var_os("NXRS_IDENTITY_CERTIFICATE_FIXTURE").unwrap(),
                )
                .join("native-verified.csr");
                std::fs::write(&path, &output[..length]).unwrap();
                let checked = std::process::Command::new(std::env::var_os("NXRS_IDENTITY_QUALIFICATION_PYTHON").unwrap()).args(["-c", "import sys;from cryptography import x509;r=x509.load_pem_x509_csr(open(sys.argv[1],'rb').read());assert r.is_signature_valid"]).arg(path).output().unwrap();
                assert!(
                    checked.status.success(),
                    "independent CSR signature verification"
                );
            }
        }
        assert!(!identity.failed());
        drop(identity);
        Observation { trace, failed }
    });
}
#[test]
fn native_certificate_and_crl_refusals_preserve_trust() {
    let _serial = serial();
    balanced();
    let mut index = 0;
    sweep("native-certificate.counts", |target| {
        let mut identity = Identity::generate(OsEntropy::open()).unwrap();
        index += 1;
        let directory = tls_controls(&mut identity, &format!("native-certificate-{index}"));
        let root = fixture_bytes(&directory, "root.der");
        let leaf = fixture_bytes(&directory, "server.der");
        let policy = certificate_policy(&directory);
        let (result, trace) = invoke(target, || identity.bind_certificate(&leaf, &root, policy));
        let failed = result.is_err();
        if let Ok(bound) = result {
            assert_eq!(bound.certificate_der().unwrap(), leaf);
        }
        assert!(!identity.failed());
        drop(identity);
        Observation { trace, failed }
    });
    sweep("native-crl.counts", |target| {
        let mut identity = Identity::generate(OsEntropy::open()).unwrap();
        index += 1;
        let directory = tls_controls(&mut identity, &format!("native-crl-{index}"));
        let root = fixture_bytes(&directory, "root.der");
        let leaf = fixture_bytes(&directory, "server.der");
        let clear = fixture_bytes(&directory, "clear.crl");
        let revoked = fixture_bytes(&directory, "revoked.crl");
        let state = revocation_state(&directory);
        let bound = identity
            .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
            .unwrap();
        let utc = certificate_policy(&directory).trusted_unix_seconds;
        let (result, trace) = invoke(target, || state.observe(&identity, &root, &revoked, utc));
        let failed = result.is_err();
        if failed {
            assert_eq!(bound.certificate_der().unwrap(), leaf);
        } else {
            assert_eq!(bound.certificate_der(), Err(Error::StaleTrust));
        }
        assert!(!identity.failed());
        drop(bound);
        drop(state);
        drop(identity);
        Observation { trace, failed }
    });
}
fn tls_attempt(target: Option<Site>, name: &str, during_handshake: bool) -> Observation {
    let mut identity = Identity::generate(OsEntropy::open()).unwrap();
    let directory = tls_controls(&mut identity, name);
    let root = fixture_bytes(&directory, "root.der");
    let leaf = fixture_bytes(&directory, "server.der");
    let clear = fixture_bytes(&directory, "clear.crl");
    let state = revocation_state(&directory);
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let mut client = tls_client(
        &directory,
        listener.local_addr().unwrap().port(),
        "valid",
        "13",
    );
    let socket = accepted(&listener);
    socket.set_nonblocking(true).unwrap();
    let dropped = Cell::new(false);
    let bound = identity
        .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
        .unwrap();
    let policy = tls_policy(&directory);
    let cancellation = TlsCancellation::default();
    let transport = AllocationIo {
        socket,
        owner: &identity,
        dropped: &dropped,
    };
    let (mut session, trace, failed) = if during_handshake {
        let mut session = bound
            .tls_server(transport, &root, policy, cancellation)
            .unwrap();
        let (result, trace) = invoke(target, || handshake(&mut session));
        let failed = result.is_err();
        if let Err(error) = result {
            assert_eq!(session.poll_handshake(), Err(error));
            assert!(session.write(b"must remain denied").is_err());
            let mut bytes = [0xa5; 32];
            assert!(session.read(&mut bytes).is_err());
            assert!(bytes.iter().all(|b| *b == 0));
        }
        (Some(session), trace, failed)
    } else {
        let (result, trace) = invoke(target, || {
            bound.tls_server(transport, &root, policy, cancellation)
        });
        let failed = result.is_err();
        (result.ok(), trace, failed)
    };
    if !failed {
        let session = session.as_mut().unwrap();
        if !during_handshake {
            handshake(session).unwrap();
        }
        response(session, &mut client);
        session.close();
    } else {
        wait_client(&mut client, false);
    }
    drop(session);
    assert!(dropped.get());
    assert!(!identity.tls_owned.get());
    assert!(!identity.failed());
    drop(state);
    drop(identity);
    Observation { trace, failed }
}
#[test]
fn native_tls_constructor_refusals_retire_io_before_lease() {
    let _serial = serial();
    balanced();
    let mut index = 0;
    sweep("native-tls-constructor.counts", |target| {
        index += 1;
        tls_attempt(target, &format!("native-constructor-{index}"), false)
    });
}
#[test]
fn native_tls_handshake_refusals_close_without_media_and_recover() {
    let _serial = serial();
    balanced();
    let mut index = 0;
    sweep("native-tls-handshake.counts", |target| {
        index += 1;
        tls_attempt(target, &format!("native-peer-{index}"), true)
    });
}

fn application_attempt(target: Option<Site>, name: &str, reading: bool) -> Observation {
    let mut identity = Identity::generate(OsEntropy::open()).unwrap();
    let directory = tls_controls(&mut identity, name);
    let root = fixture_bytes(&directory, "root.der");
    let leaf = fixture_bytes(&directory, "server.der");
    let clear = fixture_bytes(&directory, "clear.crl");
    let state = revocation_state(&directory);
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let mut client = tls_client(
        &directory,
        listener.local_addr().unwrap().port(),
        "valid",
        "13",
    );
    let socket = accepted(&listener);
    socket.set_nonblocking(true).unwrap();
    let dropped = Cell::new(false);
    let bound = identity
        .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
        .unwrap();
    let mut session = bound
        .tls_server(
            AllocationIo {
                socket,
                owner: &identity,
                dropped: &dropped,
            },
            &root,
            tls_policy(&directory),
            TlsCancellation::default(),
        )
        .unwrap();
    handshake(&mut session).unwrap();
    let mut output = [0xa5; 256];
    let (result, trace) = invoke(target, || {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(3);
        loop {
            let result = if reading {
                session.read(&mut output)
            } else {
                session.write(b"nxrs actual SDK mutual TLS response\n")
            };
            match result {
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {}
                result => break result,
            }
            assert!(std::time::Instant::now() < deadline);
            std::thread::sleep(std::time::Duration::from_millis(1));
        }
    });
    let failed = result.is_err();
    if let Ok(length) = result {
        if reading {
            assert_eq!(
                &output[..length],
                b"GET /stream HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
            );
            response(&mut session, &mut client);
        } else {
            assert_eq!(length, b"nxrs actual SDK mutual TLS response\n".len());
            wait_client(&mut client, true);
        }
        session.close();
    } else {
        if reading {
            assert!(output.iter().all(|b| *b == 0));
        }
        let sticky = session.poll_handshake().unwrap_err();
        assert_eq!(session.poll_handshake(), Err(sticky));
        assert!(session.write(b"must remain denied").is_err());
        wait_client(&mut client, false);
    }
    drop(session);
    assert!(dropped.get());
    assert!(!identity.tls_owned.get());
    assert!(!identity.failed());
    drop(state);
    drop(identity);
    Observation { trace, failed }
}
#[test]
fn native_tls_application_io_refusals_preserve_plaintext_boundaries() {
    let _serial = serial();
    balanced();
    let mut index = 0;
    sweep("native-tls-read.counts", |target| {
        index += 1;
        application_attempt(target, &format!("native-read-{index}"), true)
    });
    sweep("native-tls-write.counts", |target| {
        index += 1;
        application_attempt(target, &format!("native-write-{index}"), false)
    });
}
