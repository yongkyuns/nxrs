//! Single borrowed-key TLS connection. The SDK does not own sockets or clocks.
use super::*;
use std::io::{self, Read, Write};
use std::net::{Shutdown, TcpStream};
use std::time::{Duration, Instant};

const RECORD: usize = 4096;
const HANDSHAKE: Duration = Duration::from_secs(5);
// RFC 5280 id-kp-serverAuth/clientAuth OID contents, parsed by the SDK.
const SERVER_AUTH: &[u8] = b"\x2b\x06\x01\x05\x05\x07\x03\x01";
const CLIENT_AUTH: &[u8] = b"\x2b\x06\x01\x05\x05\x07\x03\x02";
thread_local! { static CLOCK: Cell<Option<i64>> = const { Cell::new(None) }; }

/// Native SDK clock ABI. No ambient system UTC or retained owner pointer.
/// # Safety
/// A non-null output is writable signed 64-bit SDK time storage.
#[no_mangle]
pub unsafe extern "C" fn tls_platform_time(output: *mut i64) -> i64 {
    let active = INVOCATION.try_with(|p| !p.get().is_null()).unwrap_or(false);
    let time = if active {
        CLOCK.try_with(Cell::get).ok().flatten()
    } else {
        None
    };
    let value = time.unwrap_or_else(|| {
        UNEXPECTED_CALLBACK.store(true, Ordering::Release);
        -1
    });
    if !output.is_null() {
        unsafe { *output = value };
    }
    value
}

fn timed(identity: &Identity, epoch: i64, action: impl FnOnce() -> c_int) -> Result<c_int, Error> {
    struct Reset(Option<i64>);
    impl Drop for Reset {
        fn drop(&mut self) {
            CLOCK.with(|slot| slot.set(self.0));
        }
    }
    let reset = Reset(CLOCK.with(|slot| slot.replace(Some(epoch))));
    let result = identity.sdk_status(action);
    drop(reset);
    result
}

fn bytes(owner: &Opaque, view: View) -> Result<&[u8], Error> {
    let mut size = 0;
    // SAFETY: each caller pairs a parsed/initialized SDK owner with its getter.
    // The SDK keeps these read-only buffers until the owning context is freed.
    let pointer = unsafe { view(owner.pointer(), &mut size) };
    if pointer.is_null() || size == 0 || size > 16_384 {
        return Err(Error::Authentication);
    }
    Ok(unsafe { std::slice::from_raw_parts(pointer, size) })
}

fn uri(owner: &Opaque, expected: &str) -> Result<(), Error> {
    // SAFETY: the parsed certificate owns this list and all its buffers until
    // owner is retired; no native view escapes this synchronous comparison.
    let mut entry = unsafe { tls_crt_sans(owner.pointer()) };
    let mut matched = false;
    for _ in 0..8 {
        if entry.is_null() {
            break;
        }
        let mut size = 0;
        let pointer = unsafe { tls_sequence_bytes(entry, &mut size) };
        if pointer.is_null() || size == 0 || size > 256 {
            return Err(Error::Authentication);
        }
        if unsafe { tls_sequence_tag(entry) == tls_uri_tag() } {
            let actual = unsafe { std::slice::from_raw_parts(pointer, size) };
            if matched || actual != expected.as_bytes() {
                return Err(Error::Authentication);
            }
            matched = true;
        }
        entry = unsafe { tls_sequence_next(entry) };
    }
    if matched && entry.is_null() {
        Ok(())
    } else {
        Err(Error::Authentication)
    }
}

struct Roots {
    own: Opaque,
    issuer: Opaque,
    peer: Opaque,
    crl: Opaque,
}
impl Roots {
    fn parse(identity: &Identity, certs: Certificates<'_>, epoch: i64) -> Result<Self, Error> {
        certs.policy.validate()?;
        if [certs.own, certs.issuer, certs.approved_peer]
            .iter()
            .any(|v| v.is_empty() || v.len() > RECORD)
            || certs.revocations.is_empty()
            || certs.revocations.len() > 16_384
        {
            return Err(Error::Capacity);
        }
        let mut fingerprint = [0; 32];
        let mut length = 0;
        // SAFETY: initialized SDK owner; bounded input/output remain live for
        // this synchronous hash operation. The expected pin is caller supplied.
        identity.sdk(|| unsafe {
            psa_hash_compute(
                tls_sha256_algorithm(),
                certs.issuer.as_ptr(),
                certs.issuer.len(),
                fingerprint.as_mut_ptr(),
                fingerprint.len(),
                &mut length,
            )
        })?;
        if length != fingerprint.len() || fingerprint != certs.policy.issuer_sha256 {
            return Err(Error::Authentication);
        }
        // SAFETY: exact SDK layouts, paired constructors/destructors, fresh contexts.
        let new_crt = || unsafe {
            Opaque::new(
                tls_crt_size(),
                tls_crt_align(),
                mbedtls_x509_crt_init,
                mbedtls_x509_crt_free,
            )
        };
        let roots = Self {
            own: new_crt()?,
            issuer: new_crt()?,
            peer: new_crt()?,
            crl: unsafe {
                Opaque::new(
                    tls_crl_size(),
                    tls_crl_align(),
                    mbedtls_x509_crl_init,
                    mbedtls_x509_crl_free,
                )?
            },
        };
        for (owner, der) in [
            (&roots.own, certs.own),
            (&roots.issuer, certs.issuer),
            (&roots.peer, certs.approved_peer),
        ] {
            identity.sdk(|| unsafe {
                mbedtls_x509_crt_parse_der(owner.pointer(), der.as_ptr(), der.len())
            })?;
            if bytes(owner, tls_crt_raw)? != der {
                return Err(Error::Authentication);
            }
            if unsafe {
                tls_crt_sig_md(owner.pointer()) != tls_sha256()
                    || tls_crt_sig_pk(owner.pointer()) != tls_ecdsa_signature()
            } {
                return Err(Error::Authentication);
            }
        }
        identity.sdk(|| unsafe {
            mbedtls_x509_crl_parse_der(
                roots.crl.pointer(),
                certs.revocations.as_ptr(),
                certs.revocations.len(),
            )
        })?;
        if bytes(&roots.crl, tls_crl_raw)? != certs.revocations
            || bytes(&roots.issuer, tls_crt_subject)? != bytes(&roots.issuer, tls_crt_issuer)?
            || bytes(&roots.issuer, tls_crt_subject)? != bytes(&roots.crl, tls_crl_issuer)?
            || unsafe {
                tls_crl_sig_md(roots.crl.pointer()) != tls_sha256()
                    || tls_crl_sig_pk(roots.crl.pointer()) != tls_ecdsa_signature()
                    || tls_crt_ca(roots.issuer.pointer()) != 1
                    || tls_crt_usage(roots.issuer.pointer()) != tls_ca_usage()
            }
        {
            return Err(Error::Authentication);
        }
        for (owner, purpose) in [(&roots.own, SERVER_AUTH), (&roots.peer, CLIENT_AUTH)] {
            if bytes(owner, tls_crt_issuer)? != bytes(&roots.issuer, tls_crt_subject)?
                || bytes(owner, tls_crt_eku)? != purpose
                || unsafe {
                    tls_crt_ca(owner.pointer()) != 0
                        || tls_crt_usage(owner.pointer()) != tls_sign_usage()
                        || !tls_crt_eku_next(owner.pointer()).is_null()
                }
            {
                return Err(Error::Authentication);
            }
            let mut flags = u32::MAX;
            let status = timed(identity, epoch, || unsafe {
                mbedtls_x509_crt_verify(
                    owner.pointer(),
                    roots.issuer.pointer(),
                    roots.crl.pointer(),
                    ptr::null(),
                    &mut flags,
                    None,
                    ptr::null_mut(),
                )
            })?;
            if status != 0 || flags != 0 {
                return Err(Error::Authentication);
            }
        }
        let pk = identity.pk.as_ref().ok_or(Error::Poisoned)?;
        uri(&roots.own, &certs.policy.own_uri)?;
        uri(&roots.peer, &certs.policy.peer_uri)?;
        identity.sdk(|| unsafe {
            mbedtls_pk_check_pair(tls_crt_pk(roots.own.pointer()), pk.pointer())
        })?;
        Ok(roots)
    }
    fn future(&self, identity: &Identity, epoch: i64) -> Result<bool, Error> {
        // SAFETY: pointers remain fields of their SDK owners for this call.
        let status = timed(identity, epoch, || unsafe {
            i32::from(
                [
                    tls_crt_from(self.own.pointer()),
                    tls_crt_from(self.issuer.pointer()),
                    tls_crt_from(self.peer.pointer()),
                    tls_crl_from(self.crl.pointer()),
                ]
                .iter()
                .any(|p| mbedtls_x509_time_is_future(*p) != 0),
            )
        })?;
        Ok(status != 0)
    }
    fn expired(&self, identity: &Identity, epoch: i64) -> Result<bool, Error> {
        // +1 rejects equality at notAfter/nextUpdate too; the native predicate
        // otherwise treats its exact second as still valid.
        let status = timed(identity, epoch + 1, || unsafe {
            i32::from(
                [
                    tls_crt_until(self.own.pointer()),
                    tls_crt_until(self.issuer.pointer()),
                    tls_crt_until(self.peer.pointer()),
                    tls_crl_until(self.crl.pointer()),
                ]
                .iter()
                .any(|p| mbedtls_x509_time_is_past(*p) != 0),
            )
        })?;
        Ok(status != 0)
    }
    fn cutoff(&self, identity: &Identity, time: &TimeLease) -> Result<Instant, Error> {
        let (lower, upper, _) = time.sample()?;
        if self.future(identity, lower)? || self.expired(identity, upper)? {
            return Err(Error::Time);
        }
        // Find an earlier certificate/CRL expiry within the short time lease,
        // using the SDK's parsed dates rather than another calendar parser.
        let mut low = upper;
        let mut high = time.epoch + 62;
        if !self.expired(identity, high)? {
            return Ok(time.expires);
        }
        while high - low > 1 {
            let mid = low + (high - low) / 2;
            if self.expired(identity, mid)? {
                high = mid;
            } else {
                low = mid;
            }
        }
        let until = time.observed + Duration::from_secs((high - time.epoch - 1) as u64);
        Ok(until.min(time.expires))
    }
}

struct Socket {
    stream: RefCell<TcpStream>,
    until: Cell<Instant>,
    last: Cell<Instant>,
    calls: Cell<u8>,
    bytes: Cell<usize>,
    fault: Cell<bool>,
}
impl Socket {
    fn budget(&self, size: usize) -> Option<usize> {
        let now = Instant::now();
        if now >= self.until.get() || now < self.last.get() {
            self.fault.set(true);
            return None;
        }
        self.last.set(now);
        if size == 0 || size > 8192 {
            self.fault.set(true);
            return None;
        }
        if self.calls.get() == 0 || self.bytes.get() == 0 {
            return None;
        }
        self.calls.set(self.calls.get() - 1);
        Some(size.min(self.bytes.get()))
    }
}
unsafe extern "C" fn send(context: *mut c_void, input: *const u8, size: usize) -> c_int {
    // SAFETY: BIO borrows this stable owned box only while the SSL context lives;
    // SDK-provided input is readable for size bytes. There is no async callback.
    let socket = unsafe { &*context.cast::<Socket>() };
    let Some(size) = socket.budget(size) else {
        return unsafe { tls_want_write() };
    };
    let Ok(mut stream) = socket.stream.try_borrow_mut() else {
        socket.fault.set(true);
        return unsafe { tls_io_failure() };
    };
    match stream.write(unsafe { std::slice::from_raw_parts(input, size) }) {
        Ok(n) if n > 0 => {
            socket.bytes.set(socket.bytes.get() - n);
            n as c_int
        }
        Err(e)
            if matches!(
                e.kind(),
                io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
            ) =>
        unsafe { tls_want_write() },
        _ => {
            socket.fault.set(true);
            unsafe { tls_io_failure() }
        }
    }
}
unsafe extern "C" fn recv(context: *mut c_void, output: *mut u8, size: usize) -> c_int {
    // SAFETY: same BIO lifetime; SDK supplies writable output for size bytes.
    let socket = unsafe { &*context.cast::<Socket>() };
    let Some(size) = socket.budget(size) else {
        return unsafe { tls_want_read() };
    };
    let Ok(mut stream) = socket.stream.try_borrow_mut() else {
        socket.fault.set(true);
        return unsafe { tls_io_failure() };
    };
    match stream.read(unsafe { std::slice::from_raw_parts_mut(output, size) }) {
        Ok(n) if n > 0 => {
            socket.bytes.set(socket.bytes.get() - n);
            n as c_int
        }
        Err(e)
            if matches!(
                e.kind(),
                io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
            ) =>
        unsafe { tls_want_read() },
        _ => {
            socket.fault.set(true);
            unsafe { tls_io_failure() }
        }
    }
}

/// One nonblocking TLS1.2/1.3 server connection borrowing its signing key.
/// Native crypto execution is synchronous; callers must qualify its CPU/source
/// latency before placing it on a camera capture thread. No worker is implied.
pub struct Connection<'a> {
    ssl: Opaque,
    _config: Opaque,
    roots: Roots,
    socket: Box<Socket>,
    pending: Box<[u8; RECORD]>,
    pending_size: usize,
    identity: &'a Identity,
    time: TimeLease,
    until: Instant,
    handshake_until: Instant,
    last_epoch: i64,
    ready: bool,
    closed: bool,
}
fn io_error(error: Error) -> io::Error {
    match error {
        Error::Time => io::ErrorKind::TimedOut,
        Error::Io => io::ErrorKind::ConnectionReset,
        Error::Poisoned | Error::Entropy => io::ErrorKind::NotConnected,
        Error::Authentication => io::ErrorKind::PermissionDenied,
        _ => io::ErrorKind::InvalidData,
    }
    .into()
}
impl Identity {
    pub fn accept<'a>(
        &'a mut self,
        stream: TcpStream,
        certificates: Certificates<'_>,
        time: TimeLease,
    ) -> Result<Connection<'a>, Error> {
        let (lower, upper, now) = time.sample()?;
        let roots = Roots::parse(self, certificates, upper)?;
        let until = roots.cutoff(self, &time)?;
        stream.set_nonblocking(true).map_err(|_| Error::Io)?;
        let socket = try_boxed(Socket {
            stream: RefCell::new(stream),
            until: Cell::new(until.min(now + HANDSHAKE)),
            last: Cell::new(now),
            calls: Cell::new(0),
            bytes: Cell::new(0),
            fault: Cell::new(false),
        })?;
        let pending = try_boxed([0u8; RECORD])?;
        // SAFETY: matching SDK layout/init/free operations, stable allocations.
        let config = unsafe {
            Opaque::new(
                tls_config_size(),
                tls_config_align(),
                mbedtls_ssl_config_init,
                mbedtls_ssl_config_free,
            )?
        };
        self.sdk(|| unsafe {
            mbedtls_ssl_config_defaults(
                config.pointer(),
                tls_server_endpoint(),
                tls_stream_transport(),
                tls_default_preset(),
            )
        })?;
        unsafe {
            mbedtls_ssl_conf_authmode(config.pointer(), tls_verify_required());
            tls_ssl_min_version(config.pointer(), tls_version12());
            tls_ssl_max_version(config.pointer(), tls_version13());
            mbedtls_ssl_conf_ca_chain(
                config.pointer(),
                roots.issuer.pointer(),
                roots.crl.pointer(),
            );
        }
        let pk = self.pk.as_ref().ok_or(Error::Poisoned)?;
        self.sdk(|| unsafe {
            mbedtls_ssl_conf_own_cert(config.pointer(), roots.own.pointer(), pk.pointer())
        })?;
        let ssl = unsafe {
            Opaque::new(
                tls_ssl_size(),
                tls_ssl_align(),
                mbedtls_ssl_init,
                mbedtls_ssl_free,
            )?
        };
        self.sdk(|| unsafe { mbedtls_ssl_setup(ssl.pointer(), config.pointer()) })?;
        // SAFETY: the box remains stable until BIO is detached and SSL freed.
        unsafe {
            mbedtls_ssl_set_bio(
                ssl.pointer(),
                (&*socket as *const Socket).cast_mut().cast(),
                Some(send),
                Some(recv),
                None,
            );
        }
        Ok(Connection {
            ssl,
            _config: config,
            roots,
            socket,
            pending,
            pending_size: 0,
            identity: self,
            time,
            until,
            handshake_until: now + HANDSHAKE,
            last_epoch: lower,
            ready: false,
            closed: false,
        })
    }
}
impl Connection<'_> {
    fn fail(&mut self, error: Error) -> Error {
        self.closed = true;
        self.pending.fill(0);
        self.pending_size = 0;
        let _ = self.socket.stream.borrow().shutdown(Shutdown::Both);
        error
    }
    fn epoch(&mut self) -> Result<i64, Error> {
        if self.closed {
            return Err(Error::Poisoned);
        }
        let (lower, upper, now) = self.time.sample().map_err(|e| self.fail(e))?;
        if lower < self.last_epoch
            || now < self.socket.last.get()
            || now >= self.until
            || (!self.ready && now >= self.handshake_until)
        {
            return Err(self.fail(Error::Time));
        }
        self.last_epoch = lower;
        self.socket.last.set(now);
        if self.identity.failed() {
            return Err(self.fail(Error::Poisoned));
        }
        Ok(upper)
    }
    fn operation(&mut self, action: impl FnOnce(*mut c_void) -> c_int) -> Result<c_int, Error> {
        let epoch = self.epoch()?;
        self.socket.calls.set(8);
        self.socket.bytes.set(8192);
        self.socket.until.set(if self.ready {
            self.until
        } else {
            self.until.min(self.handshake_until)
        });
        let result =
            timed(self.identity, epoch, || action(self.ssl.pointer())).map_err(|e| self.fail(e))?;
        self.epoch()?;
        if self.socket.fault.get() {
            return Err(self.fail(Error::Io));
        }
        Ok(result)
    }
    /// One SDK handshake step; returns true only after chain, CRL and pin checks.
    pub fn poll(&mut self) -> Result<bool, Error> {
        self.epoch()?;
        if self.ready {
            return Ok(true);
        }
        if unsafe { tls_handshake_over(self.ssl.pointer()) } == 0 {
            let status = self.operation(|p| unsafe { mbedtls_ssl_handshake_step(p) })?;
            if status == unsafe { tls_want_read() } || status == unsafe { tls_want_write() } {
                return Ok(false);
            }
            if status != 0 {
                return Err(self.fail(Error::Authentication));
            }
        }
        if unsafe { tls_handshake_over(self.ssl.pointer()) } == 0 {
            return Ok(false);
        }
        let pin = match (
            bytes(&self.ssl, tls_peer_raw),
            bytes(&self.roots.peer, tls_crt_raw),
        ) {
            (Ok(actual), Ok(approved)) => actual == approved,
            _ => false,
        };
        if unsafe { mbedtls_ssl_get_verify_result(self.ssl.pointer()) } != 0 || !pin {
            return Err(self.fail(Error::Authentication));
        }
        self.ready = true;
        Ok(true)
    }
    /// Refresh before expiry; a closed connection cannot be revived.
    pub fn refresh_time(&mut self, time: TimeLease) -> Result<(), Error> {
        self.epoch()?;
        let (lower, _, _) = time.sample()?;
        if lower < self.last_epoch {
            return Err(self.fail(Error::Time));
        }
        self.until = self
            .roots
            .cutoff(self.identity, &time)
            .map_err(|e| self.fail(e))?;
        self.time = time;
        self.last_epoch = lower;
        Ok(())
    }
    /// Bounded close-notify attempt. Drop aborts instead of waiting for a peer.
    pub fn close(&mut self) -> Result<bool, Error> {
        if self.pending_size != 0 {
            return Err(self.fail(Error::InvalidInput));
        }
        let status = self.operation(|p| unsafe { mbedtls_ssl_close_notify(p) })?;
        if status == unsafe { tls_want_read() } || status == unsafe { tls_want_write() } {
            return Ok(false);
        }
        self.closed = true;
        let _ = self.socket.stream.borrow().shutdown(Shutdown::Both);
        if status == 0 {
            Ok(true)
        } else {
            Err(Error::Io)
        }
    }
    fn io_status(&mut self, status: c_int) -> io::Result<usize> {
        if status > 0 {
            return Ok(status as usize);
        }
        if status == unsafe { tls_want_read() } || status == unsafe { tls_want_write() } {
            return Err(io::ErrorKind::WouldBlock.into());
        }
        if status == unsafe { tls_peer_closed() } {
            self.closed = true;
            return Ok(0);
        }
        self.fail(Error::Io);
        Err(io::ErrorKind::InvalidData.into())
    }
}
impl Read for Connection<'_> {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        if output.is_empty() {
            return Ok(0);
        }
        let size = output.len().min(RECORD);
        output[..size].fill(0);
        if !self.poll().map_err(io_error)? {
            return Err(io::ErrorKind::WouldBlock.into());
        }
        let status =
            match self.operation(|p| unsafe { mbedtls_ssl_read(p, output.as_mut_ptr(), size) }) {
                Ok(status) => status,
                Err(error) => {
                    output[..size].fill(0);
                    return Err(io_error(error));
                }
            };
        let result = self.io_status(status);
        if result.is_err() {
            output[..size].fill(0);
        }
        result
    }
}
impl Write for Connection<'_> {
    fn write(&mut self, input: &[u8]) -> io::Result<usize> {
        if input.is_empty() {
            return Ok(0);
        }
        if !self.poll().map_err(io_error)? {
            return Err(io::ErrorKind::WouldBlock.into());
        }
        if self.pending_size == 0 {
            self.pending_size = input.len().min(RECORD);
            self.pending[..self.pending_size].copy_from_slice(&input[..self.pending_size]);
        } else if input.get(..self.pending_size) != Some(&self.pending[..self.pending_size]) {
            self.fail(Error::InvalidInput);
            return Err(io::ErrorKind::InvalidInput.into());
        }
        let pointer = self.pending.as_ptr();
        let size = self.pending_size;
        let status = self
            .operation(|p| unsafe { mbedtls_ssl_write(p, pointer, size) })
            .map_err(io_error)?;
        let result = self.io_status(status);
        if let Ok(n) = result {
            if n > size {
                self.fail(Error::Crypto);
                return Err(io::ErrorKind::InvalidData.into());
            }
            self.pending.fill(0);
            self.pending_size = 0;
        }
        result
    }
    fn flush(&mut self) -> io::Result<()> {
        self.epoch().map_err(io_error)?;
        if self.pending_size == 0 {
            Ok(())
        } else {
            Err(io::ErrorKind::WouldBlock.into())
        }
    }
}
impl Drop for Connection<'_> {
    fn drop(&mut self) {
        // SDK execution and BIO callbacks are synchronous. Detach before freeing
        // SSL; fields then retire SSL/config before certificates, socket or key.
        unsafe {
            mbedtls_ssl_set_bio(self.ssl.pointer(), ptr::null_mut(), None, None, None);
        }
        let _ = self.socket.stream.borrow().shutdown(Shutdown::Both);
        self.pending.fill(0);
    }
}

#[cfg(all(test, not(target_os = "nuttx")))]
mod tests {
    use super::*;
    #[cfg(feature = "probe")]
    #[test]
    fn actual_native_clock_uses_only_call_scoped_signed64_observation() {
        unsafe extern "C" {
            fn tls_test_clock_probe(expected: i64) -> c_int;
        }
        let _serial = super::super::tests::serial();
        let mut identity = crate::open().unwrap();
        let epoch = 1_i64 << 34;
        assert_eq!(
            timed(&identity, epoch, || unsafe { tls_test_clock_probe(epoch) }),
            Ok(0)
        );
        assert_eq!(unsafe { tls_test_clock_probe(-1) }, 0);
        assert_eq!(
            identity.request("device", &mut [0; 1024]),
            Err(Error::Poisoned)
        );
        drop(identity);
        assert!(crate::open().is_ok());
    }
}
