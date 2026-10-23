//! Caller-polled SDK TLS server; owned nonblocking I/O and no ambient trust.
use super::*;
use std::fmt;
use std::io::{self, Read, Write};
use std::sync::Arc;

const BLOCK: usize = 4096;
const CALLBACK_BUFFER: usize = 8192;
const IO_BUDGET: usize = 8;
const TOTAL_IO_LIMIT: usize = 1 << 20;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TlsError {
    Identity(Error),
    Busy,
    InvalidInput,
    Cancelled,
    Deadline,
    ResourceLimit,
    Closed,
    Protocol,
    Transport(io::ErrorKind),
}
impl From<Error> for TlsError {
    fn from(error: Error) -> Self {
        Self::Identity(error)
    }
}
impl fmt::Display for TlsError {
    fn fmt(&self, out: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(out, "device TLS: {self:?}")
    }
}
impl std::error::Error for TlsError {}
impl TlsError {
    fn io(self) -> io::Error {
        let kind = match self {
            Self::Busy => io::ErrorKind::WouldBlock,
            Self::Cancelled => io::ErrorKind::ConnectionAborted,
            Self::Deadline => io::ErrorKind::TimedOut,
            Self::Closed => io::ErrorKind::UnexpectedEof,
            Self::InvalidInput => io::ErrorKind::InvalidInput,
            Self::Transport(kind) => kind,
            Self::Identity(_) => io::ErrorKind::PermissionDenied,
            Self::Protocol => io::ErrorKind::InvalidData,
            Self::ResourceLimit => io::ErrorKind::OutOfMemory,
        };
        // Keep WouldBlock and exhaustion reporting usable without another heap
        // allocation. The typed session APIs retain the sticky TLS failure.
        io::Error::from(kind)
    }
}
impl From<TlsError> for io::Error {
    fn from(error: TlsError) -> Self {
        error.io()
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TlsProgress {
    Progress,
    WantRead,
    WantWrite,
    Established,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TlsVersion {
    V1_2,
    V1_3,
}

/// Expected enrolled camera and permitted ingest identity, independently supplied.
/// Clock and protected-floor acquisition remain caller/platform responsibilities.
pub struct TlsPolicy<'a> {
    pub camera: CameraCertificatePolicy<'a>,
    pub gateway_id: &'a str,
    /// Whole handshake deadline, strictly positive and at most 30 seconds.
    pub handshake_timeout: Duration,
    /// Whole partial TLS-record deadline, positive and at most 30 seconds.
    pub record_timeout: Duration,
}
#[derive(Clone, Default)]
pub struct TlsCancellation(Arc<AtomicBool>);
impl TlsCancellation {
    pub fn cancel(&self) {
        self.0.store(true, Ordering::Release);
    }
    pub fn is_cancelled(&self) -> bool {
        self.0.load(Ordering::Acquire)
    }
}

type SendCallback = unsafe extern "C" fn(*mut c_void, *const u8, usize) -> c_int;
type ReceiveCallback = unsafe extern "C" fn(*mut c_void, *mut u8, usize) -> c_int;
type ReceiveTimeoutCallback = unsafe extern "C" fn(*mut c_void, *mut u8, usize, u32) -> c_int;
unsafe extern "C" {
    fn nxrs_di_tls_size() -> usize;
    fn nxrs_di_tls_align() -> usize;
    fn nxrs_di_tls_config_size() -> usize;
    fn nxrs_di_tls_config_align() -> usize;
    fn nxrs_di_tls_server() -> c_int;
    fn nxrs_di_tls_stream() -> c_int;
    fn nxrs_di_tls_preset() -> c_int;
    fn nxrs_di_tls_verify_required() -> c_int;
    fn nxrs_di_tls_version_12() -> c_int;
    fn nxrs_di_tls_version_13() -> c_int;
    fn nxrs_di_tls_min_version(p: *mut c_void, version: c_int);
    fn nxrs_di_tls_max_version(p: *mut c_void, version: c_int);
    fn nxrs_di_tls_handshake_over(p: *mut c_void) -> c_int;
    fn nxrs_di_tls_cipher_12() -> c_int;
    fn nxrs_di_tls_cipher_13() -> c_int;
    fn nxrs_di_tls_want_read() -> c_int;
    fn nxrs_di_tls_want_write() -> c_int;
    fn nxrs_di_tls_peer_closed() -> c_int;
    fn nxrs_di_tls_io_error() -> c_int;
    fn nxrs_di_tls_bad_certificate() -> u32;
    fn nxrs_di_tls_version(p: *const c_void) -> c_int;
    fn mbedtls_ssl_init(p: *mut c_void);
    fn mbedtls_ssl_free(p: *mut c_void);
    fn mbedtls_ssl_config_init(p: *mut c_void);
    fn mbedtls_ssl_config_free(p: *mut c_void);
    fn mbedtls_ssl_config_defaults(
        p: *mut c_void,
        endpoint: c_int,
        transport: c_int,
        preset: c_int,
    ) -> c_int;
    fn mbedtls_ssl_setup(p: *mut c_void, config: *const c_void) -> c_int;
    fn mbedtls_ssl_conf_authmode(p: *mut c_void, mode: c_int);
    fn mbedtls_ssl_conf_ciphersuites(p: *mut c_void, suites: *const c_int);
    fn mbedtls_ssl_conf_cert_profile(p: *mut c_void, profile: *const Profile);
    fn mbedtls_ssl_conf_ca_chain(p: *mut c_void, root: *mut c_void, crl: *mut c_void);
    fn mbedtls_ssl_conf_own_cert(p: *mut c_void, leaf: *mut c_void, key: *mut c_void) -> c_int;
    fn mbedtls_ssl_conf_verify(
        p: *mut c_void,
        callback: Option<VerifyCallback>,
        context: *mut c_void,
    );
    fn mbedtls_ssl_set_bio(
        p: *mut c_void,
        context: *mut c_void,
        send: Option<SendCallback>,
        receive: Option<ReceiveCallback>,
        timeout: Option<ReceiveTimeoutCallback>,
    );
    fn mbedtls_ssl_handshake_step(p: *mut c_void) -> c_int;
    fn mbedtls_ssl_get_verify_result(p: *const c_void) -> u32;
    fn mbedtls_ssl_get_ciphersuite_id_from_ssl(p: *const c_void) -> c_int;
    fn mbedtls_ssl_read(p: *mut c_void, bytes: *mut u8, length: usize) -> c_int;
    fn mbedtls_ssl_write(p: *mut c_void, bytes: *const u8, length: usize) -> c_int;
}

#[derive(Clone, Copy)]
struct Active {
    token: usize,
    pointer: *mut c_void,
}
thread_local! { static ACTIVE: Cell<Option<Active>> = const { Cell::new(None) }; }
static NEXT_TOKEN: AtomicUsize = AtomicUsize::new(1);
struct BioInvocation;
impl Drop for BioInvocation {
    fn drop(&mut self) {
        ACTIVE.with(|slot| slot.set(None));
    }
}
struct TlsLease<'key, 'entropy>(&'key Identity<'entropy>);
impl Drop for TlsLease<'_, '_> {
    fn drop(&mut self) {
        self.0.tls_owned.set(false);
    }
}
struct Peer {
    serial: Vec<u8>,
    deadline: Instant,
}
struct IoState<'key, 'entropy, 'state, T> {
    binding: CurrentCertificateBinding<'key, 'entropy, 'state>,
    transport: Option<T>,
    cancellation: TlsCancellation,
    expected_uri: String,
    utc: u64,
    local_until: u64,
    observed: Instant,
    handshake_deadline: Instant,
    record_timeout: Duration,
    read_started: Option<Instant>,
    write_started: Option<Instant>,
    peer: Option<Peer>,
    established: bool,
    token: usize,
    budget: usize,
    total_io: usize,
    callback_error: Option<TlsError>,
    pending: Vec<u8>,
}
impl<T> IoState<'_, '_, '_, T> {
    fn now(&self) -> Result<u64, TlsError> {
        self.utc
            .checked_add(self.observed.elapsed().as_secs())
            .filter(|now| *now <= 253402300799)
            .ok_or(TlsError::Deadline)
    }
    fn access(&self) -> Result<(), TlsError> {
        self.binding.remaining_validity()?;
        let utc = self.now()?;
        self.binding.state.time_remaining(utc)?;
        if utc >= self.local_until {
            return Err(TlsError::Identity(Error::Expired));
        }
        if self.cancellation.is_cancelled() {
            return Err(TlsError::Cancelled);
        }
        let now = Instant::now();
        if !self.established && now >= self.handshake_deadline {
            return Err(TlsError::Deadline);
        }
        if self
            .read_started
            .is_some_and(|start| start.elapsed() >= self.record_timeout)
            || self
                .write_started
                .is_some_and(|start| start.elapsed() >= self.record_timeout)
        {
            return Err(TlsError::Deadline);
        }
        if let Some(peer) = &self.peer {
            self.binding
                .state
                .remaining(self.binding.generation, &peer.serial)?;
            if now >= peer.deadline {
                return Err(TlsError::Identity(Error::Expired));
            }
        } else if self.established {
            return Err(TlsError::Protocol);
        }
        if self.total_io >= TOTAL_IO_LIMIT {
            return Err(TlsError::ResourceLimit);
        }
        Ok(())
    }
    fn refuse(&mut self, error: TlsError) -> c_int {
        self.callback_error = Some(error);
        // SAFETY: native constant only.
        unsafe { nxrs_di_tls_io_error() }
    }
    fn permit_io(&mut self, writing: bool) -> Result<(), c_int> {
        if let Err(error) = self.access() {
            return Err(self.refuse(error));
        }
        if self.budget == 0 {
            // SAFETY: native nonblocking callback status constants only.
            return Err(unsafe {
                if writing {
                    nxrs_di_tls_want_write()
                } else {
                    nxrs_di_tls_want_read()
                }
            });
        }
        self.budget -= 1;
        self.total_io += 1;
        Ok(())
    }
    fn verify_peer(&mut self, crt: *mut c_void) -> Result<(), TlsError> {
        self.access()?;
        let now = self.now()?;
        let policy = LeafPolicy {
            issuer_sha256: self.binding.state.issuer_sha256,
            uri: &self.expected_uri,
            ip: None,
            eku: b"\x2b\x06\x01\x05\x05\x07\x03\x02",
            trusted_unix_seconds: now,
        };
        let observed = Instant::now();
        // SAFETY: SDK callback lends this parsed leaf only during this call.
        let (serial, expires) = unsafe { inspect_leaf(crt, &policy) }?;
        self.binding
            .state
            .remaining(self.binding.generation, &serial)?;
        let deadline = observed
            .checked_add(Duration::from_secs((expires - now).min(300)))
            .ok_or(TlsError::Deadline)?;
        self.peer = Some(Peer { serial, deadline });
        Ok(())
    }
}

// The native context is an unforgeable-by-protocol, never-reused token. It is
// compared before dereferencing scoped Rust storage, including on late calls.
fn active_pointer(context: *mut c_void) -> Option<*mut c_void> {
    let active = ACTIVE.with(Cell::get);
    if let Some(active) = active.filter(|active| active.token == context as usize) {
        Some(active.pointer)
    } else {
        UNEXPECTED_CALLBACK.store(true, Ordering::Release);
        None
    }
}
unsafe extern "C" fn send<T: Read + Write>(
    context: *mut c_void,
    input: *const u8,
    length: usize,
) -> c_int {
    let Some(pointer) = active_pointer(context) else {
        // SAFETY: native constant only.
        return unsafe { nxrs_di_tls_io_error() };
    };
    // SAFETY: active scope registered this exact monomorphized live Box.
    let io = unsafe { &mut *pointer.cast::<IoState<'_, '_, '_, T>>() };
    if input.is_null() || length == 0 || length > CALLBACK_BUFFER {
        return io.refuse(TlsError::InvalidInput);
    }
    if let Err(status) = io.permit_io(true) {
        return status;
    }
    // SAFETY: actual SDK callback lends initialized bytes for this bounded call.
    let bytes = unsafe { slice::from_raw_parts(input, length) };
    let Some(transport) = io.transport.as_mut() else {
        return io.refuse(TlsError::Closed);
    };
    match transport.write(bytes) {
        Ok(n) if n > 0 && n <= length => n as c_int,
        Ok(_) => io.refuse(TlsError::Transport(io::ErrorKind::WriteZero)),
        Err(error)
            if matches!(
                error.kind(),
                io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
            ) =>
        {
            // SAFETY: native callback status only.
            unsafe { nxrs_di_tls_want_write() }
        }
        Err(error) => io.refuse(TlsError::Transport(error.kind())),
    }
}
unsafe extern "C" fn receive<T: Read + Write>(
    context: *mut c_void,
    output: *mut u8,
    length: usize,
) -> c_int {
    let Some(pointer) = active_pointer(context) else {
        // SAFETY: native constant only.
        return unsafe { nxrs_di_tls_io_error() };
    };
    // SAFETY: exact generic live Box recorded by this synchronous scope.
    let io = unsafe { &mut *pointer.cast::<IoState<'_, '_, '_, T>>() };
    if output.is_null() || length == 0 || length > CALLBACK_BUFFER {
        return io.refuse(TlsError::InvalidInput);
    }
    if let Err(status) = io.permit_io(false) {
        return status;
    }
    // SAFETY: native parser supplies writable storage of this bounded length.
    let bytes = unsafe { slice::from_raw_parts_mut(output, length) };
    bytes.fill(0);
    let Some(transport) = io.transport.as_mut() else {
        return io.refuse(TlsError::Closed);
    };
    match transport.read(bytes) {
        Ok(n) if n <= length => {
            if n > 0 && io.established && io.read_started.is_none() {
                io.read_started = Some(Instant::now());
            }
            n as c_int
        }
        Ok(_) => io.refuse(TlsError::Transport(io::ErrorKind::InvalidData)),
        Err(error)
            if matches!(
                error.kind(),
                io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
            ) =>
        {
            // SAFETY: native callback status only.
            unsafe { nxrs_di_tls_want_read() }
        }
        Err(error) => io.refuse(TlsError::Transport(error.kind())),
    }
}
unsafe extern "C" fn verify<T: Read + Write>(
    context: *mut c_void,
    crt: *mut c_void,
    depth: c_int,
    flags: *mut u32,
) -> c_int {
    // SAFETY: native constant; no parsed-object access before context validation.
    let bad = unsafe { nxrs_di_tls_bad_certificate() };
    let Some(pointer) = active_pointer(context) else {
        return -1;
    };
    // SAFETY: scoped, exact generic Box and callback-supplied output flag.
    let io = unsafe { &mut *pointer.cast::<IoState<'_, '_, '_, T>>() };
    if flags.is_null() || crt.is_null() || depth < 0 {
        io.refuse(TlsError::InvalidInput);
        return -1;
    }
    // Never clear or waive any native failure. Scope checks run before Finished.
    if depth == 0 {
        // SAFETY: actual SDK verifier's writable flag field.
        if unsafe { *flags } != 0 {
            io.callback_error = Some(TlsError::Identity(Error::Certificate));
        } else if let Err(error) = io.verify_peer(crt) {
            io.callback_error = Some(error);
            // SAFETY: native output flag remains set through verification.
            unsafe {
                *flags |= bad;
            }
        }
    }
    0
}

/// One live TLS server over owned nonblocking I/O. The I/O implementation must
/// return promptly and must not panic or call this SDK. For sockets, use
/// `CurrentCertificateBinding::tls_server_tcp` to configure nonblocking mode.
/// Read/Write drive one handshake step and return WouldBlock until authenticated.
/// No ambient trust or private
/// key export is provided. Native state retires before I/O and key borrows.
pub struct TlsServerSession<'key, 'entropy, 'state, T: Read + Write> {
    ssl: Option<Opaque>,
    config: Option<Opaque>,
    leaf: Option<Opaque>,
    root: Option<Opaque>,
    _profile: Box<Profile>,
    _ciphers: Box<[c_int; 3]>,
    io: Box<IoState<'key, 'entropy, 'state, T>>,
    version: Option<TlsVersion>,
    error: Option<TlsError>,
    read_wants_write: bool,
    lease: Option<TlsLease<'key, 'entropy>>,
}
impl<'key, 'entropy, 'state> CurrentCertificateBinding<'key, 'entropy, 'state> {
    pub fn tls_server<T: Read + Write>(
        self,
        transport: T,
        root_der: &[u8],
        policy: TlsPolicy<'_>,
        cancellation: TlsCancellation,
    ) -> Result<TlsServerSession<'key, 'entropy, 'state, T>, TlsError> {
        TlsServerSession::new(self, transport, root_der, policy, cancellation)
    }
    pub fn tls_server_tcp(
        self,
        socket: std::net::TcpStream,
        root_der: &[u8],
        policy: TlsPolicy<'_>,
        cancellation: TlsCancellation,
    ) -> Result<TlsServerSession<'key, 'entropy, 'state, std::net::TcpStream>, TlsError> {
        socket
            .set_nonblocking(true)
            .map_err(|error| TlsError::Transport(error.kind()))?;
        socket
            .set_nodelay(true)
            .map_err(|error| TlsError::Transport(error.kind()))?;
        self.tls_server(socket, root_der, policy, cancellation)
    }
}
impl<'key, 'entropy, 'state, T: Read + Write> TlsServerSession<'key, 'entropy, 'state, T> {
    fn new(
        binding: CurrentCertificateBinding<'key, 'entropy, 'state>,
        transport: T,
        root_der: &[u8],
        policy: TlsPolicy<'_>,
        cancellation: TlsCancellation,
    ) -> Result<Self, TlsError> {
        let observed = Instant::now();
        binding.remaining_validity()?;
        if cancellation.is_cancelled() {
            return Err(TlsError::Cancelled);
        }
        let camera = policy.camera;
        if !identifier(camera.site_id)
            || !identifier(camera.camera_id)
            || !identifier(policy.gateway_id)
            || camera.issuer_sha256 != binding.state.issuer_sha256
            || camera.trusted_unix_seconds == 0
            || camera.trusted_unix_seconds > 253402300799
            || camera.trusted_unix_seconds < binding.state.latest_utc.get()
            || ![policy.handshake_timeout, policy.record_timeout]
                .iter()
                .all(|limit| !limit.is_zero() && *limit <= Duration::from_secs(30))
        {
            return Err(TlsError::InvalidInput);
        }
        let owner = binding.certificate.owner;
        let token = NEXT_TOKEN
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| {
                value.checked_add(1)
            })
            .map_err(|_| TlsError::ResourceLimit)?;
        let handshake_deadline = observed
            .checked_add(policy.handshake_timeout)
            .ok_or(TlsError::InvalidInput)?;
        if owner.tls_owned.replace(true) {
            return Err(TlsError::Busy);
        }
        let lease = TlsLease(owner);
        let mut io = Box::new(IoState {
            binding,
            transport: Some(transport),
            cancellation,
            expected_uri: format!(
                "urn:nxrs:ingest:{}:{}:{}",
                camera.site_id, policy.gateway_id, camera.camera_id
            ),
            utc: camera.trusted_unix_seconds,
            local_until: u64::MAX,
            observed,
            handshake_deadline,
            record_timeout: policy.record_timeout,
            read_started: None,
            write_started: None,
            peer: None,
            established: false,
            token,
            budget: 0,
            total_io: 0,
            callback_error: None,
            pending: Vec::new(),
        });
        io.binding.state.record_time(camera.trusted_unix_seconds)?;
        io.access()?;
        owner.refresh_entropy()?;
        if root_der.is_empty()
            || root_der.len() > 2048
            || owner.hash(root_der)? != io.binding.state.issuer_sha256
        {
            return Err(TlsError::Identity(Error::Certificate));
        }
        let root = owner.parse_certificate(root_der)?;
        let leaf = owner.parse_certificate(io.binding.certificate_der()?)?;
        let camera_uri = format!("urn:nxrs:camera:{}:{}", camera.site_id, camera.camera_id);
        // SAFETY: live parsed leaf; additional policy must match held camera.
        let (_, leaf_until) = unsafe {
            inspect_leaf(
                leaf.pointer(),
                &LeafPolicy {
                    issuer_sha256: camera.issuer_sha256,
                    uri: &camera_uri,
                    ip: Some(camera.server_ip),
                    eku: b"\x2b\x06\x01\x05\x05\x07\x03\x01",
                    trusted_unix_seconds: io.now()?,
                },
            )
        }?;
        // SAFETY: actual live parsed root time field with checked ABI.
        let root_until = unsafe { unix(&*nxrs_di_crt_to(root.pointer())) }?;
        io.local_until = leaf_until.min(root_until);
        io.access()?;
        // SAFETY: exact native layouts/init/free operations from pinned headers.
        let (ssl, config, profile, ciphers) = unsafe {
            (
                Opaque::new(
                    nxrs_di_tls_size(),
                    nxrs_di_tls_align(),
                    mbedtls_ssl_init,
                    mbedtls_ssl_free,
                )?,
                Opaque::new(
                    nxrs_di_tls_config_size(),
                    nxrs_di_tls_config_align(),
                    mbedtls_ssl_config_init,
                    mbedtls_ssl_config_free,
                )?,
                Box::new(Profile {
                    hashes: nxrs_di_profile_sha256(),
                    keys: nxrs_di_profile_ec_keys(),
                    curves: nxrs_di_profile_p256(),
                    minimum_rsa_bits: u32::MAX,
                }),
                Box::new([nxrs_di_tls_cipher_13(), nxrs_di_tls_cipher_12(), 0]),
            )
        };
        let session = Self {
            ssl: Some(ssl),
            config: Some(config),
            leaf: Some(leaf),
            root: Some(root),
            _profile: profile,
            _ciphers: ciphers,
            io,
            version: None,
            error: None,
            read_wants_write: false,
            lease: Some(lease),
        };
        let conf = session.config.as_ref().ok_or(TlsError::Closed)?.pointer();
        let ssl = session.ssl.as_ref().ok_or(TlsError::Closed)?.pointer();
        // SAFETY: contexts and all referenced buffers remain stable in owned Boxes.
        owner.sdk(|| unsafe {
            mbedtls_ssl_config_defaults(
                conf,
                nxrs_di_tls_server(),
                nxrs_di_tls_stream(),
                nxrs_di_tls_preset(),
            )
        })?;
        unsafe {
            mbedtls_ssl_conf_authmode(conf, nxrs_di_tls_verify_required());
            nxrs_di_tls_min_version(conf, nxrs_di_tls_version_12());
            nxrs_di_tls_max_version(conf, nxrs_di_tls_version_13());
            mbedtls_ssl_conf_ciphersuites(conf, session._ciphers.as_ptr());
            mbedtls_ssl_conf_cert_profile(conf, &*session._profile);
            mbedtls_ssl_conf_ca_chain(
                conf,
                session.root.as_ref().ok_or(TlsError::Closed)?.pointer(),
                ptr::null_mut(),
            );
            mbedtls_ssl_conf_verify(conf, Some(verify::<T>), token as *mut c_void);
        }
        // SAFETY: same owner wrapped key, held certificate and config stay live.
        owner.sdk(|| unsafe {
            mbedtls_ssl_conf_own_cert(
                conf,
                session.leaf.as_ref().unwrap().pointer(),
                owner.pk.as_ref().unwrap().pointer(),
            )
        })?;
        owner.sdk(|| unsafe { mbedtls_ssl_setup(ssl, conf) })?;
        // SAFETY: native SDK retains only the token and private monomorphized callbacks.
        unsafe {
            mbedtls_ssl_set_bio(
                ssl,
                token as *mut c_void,
                Some(send::<T>),
                Some(receive::<T>),
                None,
            );
        }
        session.io.access()?;
        Ok(session)
    }
    fn terminate(&mut self, error: TlsError) -> TlsError {
        self.error.get_or_insert(error);
        drop(self.ssl.take());
        drop(self.config.take());
        drop(self.leaf.take());
        drop(self.root.take());
        self.io.pending.fill(0);
        self.io.pending.clear();
        drop(self.io.transport.take());
        drop(self.lease.take());
        error
    }
    fn operation(
        &mut self,
        operation: impl FnOnce(*mut c_void) -> c_int,
    ) -> Result<c_int, TlsError> {
        if let Some(error) = self.error {
            return Err(error);
        }
        if let Err(error) = self.io.access() {
            return Err(self.terminate(error));
        }
        let now = self.io.now()?;
        TRUSTED_TIME.with(|slot| {
            if slot.get().is_some() {
                Err(TlsError::Busy)
            } else {
                slot.set(Some(now as i64));
                Ok(())
            }
        })?;
        let time = TimeInvocation;
        self.io.budget = IO_BUDGET;
        self.io.callback_error = None;
        ACTIVE.with(|slot| {
            if slot.get().is_some() {
                Err(TlsError::Busy)
            } else {
                slot.set(Some(Active {
                    token: self.io.token,
                    pointer: (&mut *self.io as *mut IoState<'_, '_, '_, T>).cast(),
                }));
                Ok(())
            }
        })?;
        let bio = BioInvocation;
        let ssl = self.ssl.as_ref().ok_or(TlsError::Closed)?.pointer();
        let mut status = 0;
        let outcome = self.io.binding.certificate.owner.sdk(|| {
            status = operation(ssl);
            0
        });
        drop(bio);
        drop(time);
        if let Err(error) = outcome {
            return Err(self.terminate(error.into()));
        }
        if let Some(error) = self.io.callback_error {
            return Err(self.terminate(error));
        }
        if let Err(error) = self.io.access() {
            return Err(self.terminate(error));
        }
        Ok(status)
    }
    pub fn poll_handshake(&mut self) -> Result<TlsProgress, TlsError> {
        if let Some(error) = self.error {
            return Err(error);
        }
        if self.io.established {
            if let Err(error) = self.io.access() {
                return Err(self.terminate(error));
            }
            return Ok(TlsProgress::Established);
        }
        // SAFETY: operation supplies the live, initialized native SSL context.
        let status = self.operation(|ssl| unsafe { mbedtls_ssl_handshake_step(ssl) })?;
        // SAFETY: native status constants only.
        unsafe {
            if status == nxrs_di_tls_want_read() {
                return Ok(TlsProgress::WantRead);
            }
            if status == nxrs_di_tls_want_write() {
                return Ok(TlsProgress::WantWrite);
            }
        }
        if status != 0 {
            return Err(self.terminate(TlsError::Protocol));
        }
        let ssl = self.ssl.as_ref().ok_or(TlsError::Closed)?.pointer();
        // SAFETY: live native fields/getters, no callback or borrowed output.
        unsafe {
            if nxrs_di_tls_handshake_over(ssl) == 0 {
                return Ok(TlsProgress::Progress);
            }
            let version = nxrs_di_tls_version(ssl);
            let suite = mbedtls_ssl_get_ciphersuite_id_from_ssl(ssl);
            if mbedtls_ssl_get_verify_result(ssl) != 0 || self.io.peer.is_none() {
                return Err(self.terminate(TlsError::Identity(Error::Certificate)));
            }
            self.version = Some(
                if version == nxrs_di_tls_version_13() && suite == nxrs_di_tls_cipher_13() {
                    TlsVersion::V1_3
                } else if version == nxrs_di_tls_version_12() && suite == nxrs_di_tls_cipher_12() {
                    TlsVersion::V1_2
                } else {
                    return Err(self.terminate(TlsError::Protocol));
                },
            );
        }
        self.io.established = true;
        if let Err(error) = self.io.access() {
            return Err(self.terminate(error));
        }
        Ok(TlsProgress::Established)
    }
    pub fn negotiated_version(&self) -> Result<Option<TlsVersion>, TlsError> {
        if let Some(error) = self.error {
            return Err(error);
        }
        self.io.access()?;
        Ok(self.version)
    }
    pub fn remaining_validity(&self) -> Result<Duration, TlsError> {
        if let Some(error) = self.error {
            return Err(error);
        }
        self.io.access()?;
        let now = self.io.now()?;
        let local = self
            .io
            .binding
            .remaining_validity()?
            .min(self.io.binding.state.time_remaining(now)?)
            .min(Duration::from_secs(
                self.io
                    .local_until
                    .checked_sub(now)
                    .ok_or(TlsError::Identity(Error::Expired))?,
            ));
        let deadline = self
            .io
            .peer
            .as_ref()
            .map(|peer| peer.deadline)
            .unwrap_or(self.io.handshake_deadline);
        Ok(local.min(
            deadline
                .checked_duration_since(Instant::now())
                .ok_or(TlsError::Deadline)?,
        ))
    }
    // Read/Write drive one bounded handshake step while application admission
    // is pending. This lets an existing nonblocking stream owner use the same
    // HTTP state machine, without exposing pre-authentication plaintext.
    fn ready_for_io(&mut self) -> io::Result<()> {
        if let Some(error) = self.error {
            return Err(error.io());
        }
        if !self.io.established {
            match self.poll_handshake().map_err(TlsError::io)? {
                TlsProgress::Established => {}
                _ => return Err(TlsError::Busy.io()),
            }
        }
        if let Err(error) = self.io.access() {
            return Err(self.terminate(error).io());
        }
        Ok(())
    }
    /// Close owned I/O without a blocking native close-notify exchange.
    pub fn close(&mut self) {
        self.terminate(TlsError::Closed);
    }
}
impl<T: Read + Write> Read for TlsServerSession<'_, '_, '_, T> {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        output.fill(0);
        self.ready_for_io()?;
        if !self.io.pending.is_empty() {
            return Err(TlsError::Busy.io());
        }
        if output.is_empty() {
            return Ok(0);
        }
        let length = output.len().min(BLOCK);
        // SAFETY: exclusive bounded caller output, initialized live SDK context.
        let result =
            self.operation(|ssl| unsafe { mbedtls_ssl_read(ssl, output.as_mut_ptr(), length) });
        let status = match result {
            Ok(status) => status,
            Err(error) => {
                output.fill(0);
                return Err(error.io());
            }
        };
        if status > 0 && status as usize <= length {
            self.io.read_started = None;
            self.read_wants_write = false;
            return Ok(status as usize);
        }
        output.fill(0);
        // SAFETY: native return constants only.
        unsafe {
            if status == nxrs_di_tls_want_read() {
                self.read_wants_write = false;
                return Err(TlsError::Busy.io());
            }
            if status == nxrs_di_tls_want_write() {
                self.read_wants_write = true;
                return Err(TlsError::Busy.io());
            }
            if status == 0 || status == nxrs_di_tls_peer_closed() {
                self.terminate(TlsError::Closed);
                return Ok(0);
            }
        }
        output.fill(0);
        Err(self.terminate(TlsError::Protocol).io())
    }
}
impl<T: Read + Write> Write for TlsServerSession<'_, '_, '_, T> {
    fn write(&mut self, input: &[u8]) -> io::Result<usize> {
        self.ready_for_io()?;
        if self.read_wants_write {
            return Err(TlsError::Busy.io());
        }
        if input.is_empty() {
            return Ok(0);
        }
        let input = &input[..input.len().min(BLOCK)];
        if self.io.pending.is_empty() {
            if self.io.pending.try_reserve_exact(input.len()).is_err() {
                return Err(self.terminate(TlsError::ResourceLimit).io());
            }
            self.io.pending.extend_from_slice(input);
            self.io.write_started = Some(Instant::now());
        } else if self.io.pending != input {
            return Err(self.terminate(TlsError::InvalidInput).io());
        }
        let bytes = self.io.pending.as_ptr();
        let length = self.io.pending.len();
        // SAFETY: stable owned pending buffer cannot change during this call/retry.
        let status = self
            .operation(|ssl| unsafe { mbedtls_ssl_write(ssl, bytes, length) })
            .map_err(TlsError::io)?;
        if status > 0 && status as usize <= length {
            self.io.pending.fill(0);
            self.io.pending.clear();
            self.io.write_started = None;
            return Ok(status as usize);
        }
        // SAFETY: native nonblocking status constants only.
        unsafe {
            if status == nxrs_di_tls_want_read() || status == nxrs_di_tls_want_write() {
                return Err(TlsError::Busy.io());
            }
        }
        Err(self.terminate(TlsError::Protocol).io())
    }
    fn flush(&mut self) -> io::Result<()> {
        if let Some(error) = self.error {
            return Err(error.io());
        }
        if let Err(error) = self.io.access() {
            return Err(self.terminate(error).io());
        }
        if self.io.pending.is_empty() {
            Ok(())
        } else {
            Err(TlsError::Busy.io())
        }
    }
}
impl<T: Read + Write> Drop for TlsServerSession<'_, '_, '_, T> {
    fn drop(&mut self) {
        self.terminate(TlsError::Closed);
    }
}

// Test-only typed Rust controls call the actual registered native callbacks.
// No C exports, alternate production callback or state-reset bypass is added.
#[cfg(all(test, nxrs_device_identity_qualification))]
pub(crate) struct BioTicket(usize);
#[cfg(all(test, nxrs_device_identity_qualification))]
impl<T: Read + Write> TlsServerSession<'_, '_, '_, T> {
    pub(crate) fn qualification_bio_ticket(&self) -> BioTicket {
        BioTicket(self.io.token)
    }
    pub(crate) fn qualification_valid_bio(&mut self) -> bool {
        let before = self.io.total_io;
        self.io.budget = IO_BUDGET;
        assert!(ACTIVE.with(|slot| slot.get().is_none()));
        ACTIVE.with(|slot| {
            slot.set(Some(Active {
                token: self.io.token,
                pointer: (&mut *self.io as *mut IoState<'_, '_, '_, T>).cast(),
            }))
        });
        let scope = BioInvocation;
        // SAFETY: same active Box/type/token and fixed initialized test bytes.
        let status = unsafe { send::<T>(self.io.token as *mut c_void, b"callback".as_ptr(), 8) };
        drop(scope);
        status == 8 && self.io.total_io == before + 1 && !self.io.binding.certificate.owner.failed()
    }
    pub(crate) fn qualification_late_bio(&mut self, ticket: BioTicket, during_scope: bool) -> bool {
        let before = self.io.total_io;
        if during_scope {
            assert_ne!(ticket.0, self.io.token);
            assert!(ACTIVE.with(|slot| slot.get().is_none()));
            ACTIVE.with(|slot| {
                slot.set(Some(Active {
                    token: self.io.token,
                    pointer: (&mut *self.io as *mut IoState<'_, '_, '_, T>).cast(),
                }))
            });
        }
        let scope = BioInvocation;
        // SAFETY: stale/unscoped token must refuse before touching output or Box.
        let status = unsafe { receive::<T>(ticket.0 as *mut c_void, ptr::null_mut(), usize::MAX) };
        drop(scope);
        // SAFETY: header constant only.
        status == unsafe { nxrs_di_tls_io_error() }
            && self.io.total_io == before
            && self.io.binding.certificate.owner.failed()
    }
    pub(crate) fn qualification_foreign_bio(&mut self) -> bool
    where
        T: 'static,
    {
        let before = self.io.total_io;
        let token = self.io.token;
        assert!(ACTIVE.with(|slot| slot.get().is_none()));
        ACTIVE.with(|slot| {
            slot.set(Some(Active {
                token,
                pointer: (&mut *self.io as *mut IoState<'_, '_, '_, T>).cast(),
            }))
        });
        let scope = BioInvocation;
        let status = std::thread::spawn(move || {
            // SAFETY: foreign thread has no scope and must refuse before any dereference.
            unsafe { send::<T>(token as *mut c_void, ptr::null(), usize::MAX) }
        })
        .join()
        .unwrap();
        drop(scope);
        // SAFETY: header constant only.
        status == unsafe { nxrs_di_tls_io_error() }
            && self.io.total_io == before
            && self.io.binding.certificate.owner.failed()
    }
}
