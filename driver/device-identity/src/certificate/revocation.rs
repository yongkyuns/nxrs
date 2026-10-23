//! Strict direct, complete issuer CRLs. No storage or trusted-clock fallback.
use super::*;

const MAX_CRL: usize = 32768;
const MAX_ENTRIES: usize = 512;
const MAX_GENERATION: u64 = 1_000_000_000;
const DAY: u64 = 86400;

unsafe extern "C" {
    fn nxrs_di_crt_subject(p: *const c_void) -> *const c_void;
    fn nxrs_di_crt_ski(p: *const c_void) -> *const c_void;
    fn nxrs_di_usage_ca() -> u32;
    fn nxrs_di_p256_public_type() -> u16;
    fn nxrs_di_signature_ecdsa() -> c_int;
    fn mbedtls_pk_get_key_type(p: *const c_void) -> u16;
    fn mbedtls_pk_get_bitlen(p: *const c_void) -> usize;
    fn mbedtls_pk_verify_ext(
        algorithm: c_int,
        pk: *mut c_void,
        hash_algorithm: c_int,
        hash: *const u8,
        hash_length: usize,
        signature: *const u8,
        signature_length: usize,
    ) -> c_int;
    fn nxrs_di_crl_size() -> usize;
    fn nxrs_di_crl_align() -> usize;
    fn mbedtls_x509_crl_init(p: *mut c_void);
    fn mbedtls_x509_crl_free(p: *mut c_void);
    fn mbedtls_x509_crl_parse_der(p: *mut c_void, der: *const u8, length: usize) -> c_int;
    fn nxrs_di_crl_raw_length(p: *const c_void) -> usize;
    fn nxrs_di_crl_next(p: *const c_void) -> *const c_void;
    fn nxrs_di_crl_from(p: *const c_void) -> *const SdkTime;
    fn nxrs_di_crl_to(p: *const c_void) -> *const SdkTime;
    fn nxrs_di_crl_tbs(p: *const c_void) -> *const c_void;
    fn nxrs_di_crl_signature(p: *const c_void) -> *const c_void;
    fn nxrs_di_crl_entries(p: *const c_void) -> *const c_void;
    fn nxrs_di_crl_entry_next(p: *const c_void) -> *const c_void;
    fn nxrs_di_crl_entry_serial(p: *const c_void) -> *const c_void;
    fn nxrs_di_crl_entry_date(p: *const c_void) -> *const SdkTime;
}

struct Snapshot {
    generation: u64,
    tbs_sha256: [u8; 32],
    this_update: u64,
    revoked: Vec<Vec<u8>>,
    deadline: Instant,
}

/// In-memory revocation policy for one independently pinned enrollment issuer.
/// The caller must acquire and persist the protected floor and provide fresh
/// trusted UTC. This cache does not make a floor durable or establish time trust.
/// Recreating it loses observed state; production recovery needs trusted storage.
/// It is confined to its owning thread, like the live SDK key owner.
pub struct RevocationState {
    issuer_sha256: [u8; 32],
    floor: Cell<u64>,
    latest_utc: Cell<u64>,
    current: RefCell<Option<Snapshot>>,
    poisoned: Cell<bool>,
    _thread: PhantomData<Rc<()>>,
}
impl RevocationState {
    pub fn from_trusted_floor(
        issuer_sha256: [u8; 32],
        minimum_generation: u64,
    ) -> Result<Self, Error> {
        if !(1..=MAX_GENERATION).contains(&minimum_generation) {
            return Err(Error::InvalidInput);
        }
        Ok(Self {
            issuer_sha256,
            floor: Cell::new(minimum_generation),
            latest_utc: Cell::new(0),
            current: RefCell::new(None),
            poisoned: Cell::new(false),
            _thread: PhantomData,
        })
    }
    /// Supply a newly acquired protected floor. Lower floors never reopen access.
    /// This operation does not write any storage or clear a poisoned state.
    pub fn raise_trusted_floor(&self, minimum_generation: u64) -> Result<(), Error> {
        if self.poisoned.get() {
            return Err(Error::Poisoned);
        }
        if !(1..=MAX_GENERATION).contains(&minimum_generation) {
            return Err(Error::InvalidInput);
        }
        if minimum_generation < self.floor.get() {
            return Err(Error::Rollback);
        }
        self.floor.set(minimum_generation);
        Ok(())
    }
    /// Verify and accept a complete issuer CRL, independently of any leaf.
    /// Malformed, unsigned and replayed updates leave healthy state unchanged.
    /// Authenticated equivocation, time regression or removal of an observed
    /// revocation poisons this cache and all its existing bindings permanently.
    pub fn observe(
        &self,
        identity: &Identity<'_>,
        root_der: &[u8],
        crl_der: &[u8],
        trusted_unix_seconds: u64,
    ) -> Result<u64, Error> {
        if self.poisoned.get() || identity.failed() {
            return Err(Error::Poisoned);
        }
        if trusted_unix_seconds == 0
            || trusted_unix_seconds > 253402300799
            || trusted_unix_seconds < self.latest_utc.get()
        {
            return Err(Error::InvalidInput);
        }
        let candidate =
            identity.verify_crl(root_der, crl_der, self.issuer_sha256, trusted_unix_seconds)?;
        if candidate.generation < self.floor.get() {
            return Err(Error::Rollback);
        }
        let mut current = self.current.try_borrow_mut().map_err(|_| Error::Busy)?;
        if let Some(previous) = current.as_mut() {
            if candidate.generation < previous.generation {
                return Err(Error::Rollback);
            }
            if (candidate.generation == previous.generation
                && candidate.tbs_sha256 != previous.tbs_sha256)
                || candidate.this_update < previous.this_update
                || !previous
                    .revoked
                    .iter()
                    .all(|serial| candidate.revoked.binary_search(serial).is_ok())
            {
                self.poisoned.set(true);
                return Err(Error::Poisoned);
            }
            if candidate.generation == previous.generation {
                // Replaying/re-signing identical signed contents never extends
                // the original monotonic deadline, including after expiry.
                previous.deadline = previous.deadline.min(candidate.deadline);
                self.latest_utc.set(trusted_unix_seconds);
                return Ok(previous.generation);
            }
        }
        let generation = candidate.generation;
        *current = Some(candidate);
        self.floor.set(generation);
        self.latest_utc.set(trusted_unix_seconds);
        Ok(generation)
    }
    fn remaining(&self, generation: u64, serial: &[u8]) -> Result<Duration, Error> {
        if self.poisoned.get() {
            return Err(Error::Poisoned);
        }
        let current = self.current.try_borrow().map_err(|_| Error::Busy)?;
        let current = current.as_ref().ok_or(Error::StaleTrust)?;
        if current.generation != generation || generation < self.floor.get() {
            return Err(Error::StaleTrust);
        }
        if current
            .revoked
            .binary_search_by(|value| value.as_slice().cmp(serial))
            .is_ok()
        {
            return Err(Error::Revoked);
        }
        current
            .deadline
            .checked_duration_since(Instant::now())
            .filter(|remaining| !remaining.is_zero())
            .ok_or(Error::Expired)
    }
}

/// A live key/certificate binding tied to one accepted CRL generation.
/// New accepted generations invalidate it, requiring admission again. Every
/// access checks owner health, certificate/CRL expiry, revocation and the floor.
/// This is a software admission guard; device TLS and durable trust are separate.
pub struct CurrentCertificateBinding<'key, 'entropy, 'state> {
    certificate: CertificateBinding<'key, 'entropy>,
    state: &'state RevocationState,
    generation: u64,
}
impl CurrentCertificateBinding<'_, '_, '_> {
    pub fn remaining_validity(&self) -> Result<Duration, Error> {
        let certificate = self.certificate.remaining_validity()?;
        Ok(certificate.min(
            self.state
                .remaining(self.generation, &self.certificate.serial)?,
        ))
    }
    pub fn certificate_der(&self) -> Result<&[u8], Error> {
        self.remaining_validity()?;
        Ok(&self.certificate.der)
    }
    pub fn generation(&self) -> Result<u64, Error> {
        self.remaining_validity()?;
        Ok(self.generation)
    }
}

impl<'entropy> Identity<'entropy> {
    pub fn bind_current_certificate<'key, 'state>(
        &'key self,
        leaf_der: &[u8],
        root_der: &[u8],
        policy: CameraCertificatePolicy<'_>,
        state: &'state RevocationState,
        crl_der: &[u8],
    ) -> Result<CurrentCertificateBinding<'key, 'entropy, 'state>, Error> {
        if policy.issuer_sha256 != state.issuer_sha256 {
            return Err(Error::Certificate);
        }
        // Accept a valid revocation update before considering leaf admission.
        // A now-revoked or otherwise invalid leaf must not suppress that update.
        let generation = state.observe(self, root_der, crl_der, policy.trusted_unix_seconds)?;
        let certificate = self.bind_certificate(leaf_der, root_der, policy)?;
        let binding = CurrentCertificateBinding {
            certificate,
            state,
            generation,
        };
        binding.remaining_validity()?;
        Ok(binding)
    }
    fn hash(&self, bytes: &[u8]) -> Result<[u8; 32], Error> {
        let mut result = [0; 32];
        let mut length = 0;
        // SAFETY: actual algorithm constant; bounded exclusive output.
        self.certificate_sdk(|| unsafe {
            psa_hash_compute(
                nxrs_di_psa_sha256(),
                bytes.as_ptr(),
                bytes.len(),
                result.as_mut_ptr(),
                result.len(),
                &mut length,
            )
        })?;
        if length != result.len() {
            return Err(Error::Certificate);
        }
        Ok(result)
    }
    fn verify_crl(
        &self,
        root_der: &[u8],
        crl_der: &[u8],
        pin: [u8; 32],
        now: u64,
    ) -> Result<Snapshot, Error> {
        let observed = Instant::now();
        // Preflight before SDK CRL allocations: total bytes, issuer name,
        // extension scope, count and each entry's canonical serial are bounded.
        let preflight = preflight(crl_der)?;
        if root_der.is_empty() || root_der.len() > 2048 {
            return Err(Error::Capacity);
        }
        if self.hash(root_der)? != pin {
            return Err(Error::Certificate);
        }
        let root = self.parse_certificate(root_der)?;
        let r = root.pointer();
        // SAFETY: getters view a live successfully parsed root and native flags.
        let pk = unsafe {
            let pk = nxrs_di_crt_pk(r);
            if nxrs_di_crt_ca(r) != 1
                || nxrs_di_crt_path_length(r) != 1
                || nxrs_di_crt_usage(r) != nxrs_di_usage_signature() | nxrs_di_usage_ca()
                || mbedtls_pk_get_key_type(pk) != nxrs_di_p256_public_type()
                || mbedtls_pk_get_bitlen(pk) != 256
                || buffer(nxrs_di_crt_subject(r), 512)? != preflight.issuer
                || buffer(nxrs_di_crt_ski(r), 20)? != preflight.aki
            {
                return Err(Error::Certificate);
            }
            pk
        };
        // SAFETY: root remains live and ABI of its SDK dates is asserted in C.
        let (root_from, root_to) =
            unsafe { (unix(&*nxrs_di_crt_from(r))?, unix(&*nxrs_di_crt_to(r))?) };
        if root_from > now || root_to <= now {
            return Err(Error::Certificate);
        }
        // SAFETY: exact SDK CRL layout and init/free operations.
        let crl = unsafe {
            Opaque::new(
                nxrs_di_crl_size(),
                nxrs_di_crl_align(),
                mbedtls_x509_crl_init,
                mbedtls_x509_crl_free,
            )?
        };
        self.certificate_sdk(|| {
            // SAFETY: parser copies the borrowed, preflight-bounded DER.
            unsafe { mbedtls_x509_crl_parse_der(crl.pointer(), crl_der.as_ptr(), crl_der.len()) }
        })?;
        let c = crl.pointer();
        // SAFETY: initialized parsed native CRL, checked before field traversal.
        let (from, to) = unsafe {
            if nxrs_di_crl_raw_length(c) != crl_der.len()
                || !nxrs_di_crl_next(c).is_null()
                || buffer(nxrs_di_crl_tbs(c), MAX_CRL)? != preflight.tbs
                || buffer(nxrs_di_crl_signature(c), 72)? != preflight.signature
            {
                return Err(Error::Certificate);
            }
            (unix(&*nxrs_di_crl_from(c))?, unix(&*nxrs_di_crl_to(c))?)
        };
        if from > now
            || now - from > DAY
            || to <= now
            || to <= from
            || to - from > DAY
            || to > root_to
        {
            return Err(Error::Certificate);
        }
        let tbs_sha256 = self.hash(preflight.tbs)?;
        // SAFETY: actual typed SDK verification; root and slices stay live.
        // Algorithms were independently constrained to ECDSA-with-SHA256 in
        // both signed and outer DER before native parsing or state mutation.
        self.certificate_sdk(|| unsafe {
            mbedtls_pk_verify_ext(
                nxrs_di_signature_ecdsa(),
                pk,
                nxrs_di_sha256(),
                tbs_sha256.as_ptr(),
                tbs_sha256.len(),
                preflight.signature.as_ptr(),
                preflight.signature.len(),
            )
        })?;
        // Cross-check all SDK entries against the bounded preflight traversal.
        // SAFETY: embedded first entry of the successfully parsed, live CRL.
        let mut entry = unsafe { nxrs_di_crl_entries(c) };
        for serial in &preflight.serials {
            if entry.is_null() {
                return Err(Error::Certificate);
            }
            // SAFETY: follows actual parser-owned next links, at most 512.
            unsafe {
                if positive_integer(buffer(nxrs_di_crl_entry_serial(entry), 20)?)? != *serial
                    || unix(&*nxrs_di_crl_entry_date(entry))? > from
                {
                    return Err(Error::Certificate);
                }
                entry = nxrs_di_crl_entry_next(entry);
            }
        }
        if preflight.serials.is_empty() {
            // SDK represents an empty CRL with one empty embedded entry.
            // SAFETY: embedded entry remains live; no bytes are dereferenced.
            if unsafe {
                nxrs_di_buf_length(nxrs_di_crl_entry_serial(entry)) != 0
                    || !nxrs_di_crl_entry_next(entry).is_null()
            } {
                return Err(Error::Certificate);
            }
        } else if !entry.is_null() {
            return Err(Error::Certificate);
        }
        let mut revoked: Vec<Vec<u8>> = preflight
            .serials
            .iter()
            .map(|serial| serial.to_vec())
            .collect();
        revoked.sort_unstable();
        let deadline = observed
            .checked_add(Duration::from_secs(to - now))
            .ok_or(Error::Certificate)?;
        if deadline <= Instant::now() {
            return Err(Error::Expired);
        }
        Ok(Snapshot {
            generation: preflight.generation,
            tbs_sha256,
            this_update: from,
            revoked,
            deadline,
        })
    }
}

// Minimal definite, canonical DER envelope reader. This is a resource/scope
// preflight, not a cryptographic implementation. The actual SDK still parses
// every admitted CRL and verifies its P-256/SHA-256 signature.
struct Der<'a>(&'a [u8]);
struct Tlv<'a> {
    tag: u8,
    full: &'a [u8],
    body: &'a [u8],
}
impl<'a> Der<'a> {
    fn item(&mut self) -> Result<Tlv<'a>, Error> {
        let input = self.0;
        if input.len() < 2 || input[0] & 0x1f == 0x1f {
            return Err(Error::Certificate);
        }
        let mut header = 2;
        let mut length = usize::from(input[1]);
        if length & 0x80 != 0 {
            let count = length & 0x7f;
            if count == 0 || count > 2 || input.len() < 2 + count || input[2] == 0 {
                return Err(Error::Certificate);
            }
            length = 0;
            for byte in &input[2..2 + count] {
                length = length * 256 + usize::from(*byte);
            }
            if length < 128 {
                return Err(Error::Certificate);
            }
            header += count;
        }
        let end = header + length;
        if end > input.len() {
            return Err(Error::Certificate);
        }
        self.0 = &input[end..];
        Ok(Tlv {
            tag: input[0],
            full: &input[..end],
            body: &input[header..end],
        })
    }
    fn take(&mut self, tag: u8) -> Result<Tlv<'a>, Error> {
        let item = self.item()?;
        if item.tag != tag {
            return Err(Error::Certificate);
        }
        Ok(item)
    }
    fn done(self) -> Result<(), Error> {
        if self.0.is_empty() {
            Ok(())
        } else {
            Err(Error::Certificate)
        }
    }
    fn date(&mut self) -> Result<(), Error> {
        let date = self.item()?;
        let length = match date.tag {
            0x17 => 13,
            0x18 => 15,
            _ => return Err(Error::Certificate),
        };
        if date.body.len() != length
            || date.body[length - 1] != b'Z'
            || !date.body[..length - 1].iter().all(u8::is_ascii_digit)
        {
            return Err(Error::Certificate);
        }
        Ok(())
    }
    fn algorithm(&mut self) -> Result<(), Error> {
        let mut algorithm = Der(self.take(0x30)?.body);
        if algorithm.take(0x06)?.body != b"\x2a\x86\x48\xce\x3d\x04\x03\x02" {
            return Err(Error::Certificate);
        }
        algorithm.done()
    }
}
struct Preflight<'a> {
    tbs: &'a [u8],
    issuer: &'a [u8],
    signature: &'a [u8],
    aki: &'a [u8],
    generation: u64,
    serials: Vec<&'a [u8]>,
}
fn preflight(der: &[u8]) -> Result<Preflight<'_>, Error> {
    if der.is_empty() || der.len() > MAX_CRL {
        return Err(Error::Capacity);
    }
    let mut input = Der(der);
    let mut outer = Der(input.take(0x30)?.body);
    input.done()?;
    let signed = outer.take(0x30)?;
    let mut tbs = Der(signed.body);
    if tbs.take(0x02)?.body != [1] {
        return Err(Error::Certificate);
    }
    tbs.algorithm()?;
    let issuer = tbs.take(0x30)?.full;
    if issuer.len() > 512 {
        return Err(Error::Capacity);
    }
    tbs.date()?;
    tbs.date()?;
    let mut serials = Vec::new();
    if tbs.0.first() == Some(&0x30) {
        let mut entries = Der(tbs.take(0x30)?.body);
        if entries.0.is_empty() {
            return Err(Error::Certificate);
        }
        while !entries.0.is_empty() {
            if serials.len() == MAX_ENTRIES {
                return Err(Error::Capacity);
            }
            let mut entry = Der(entries.take(0x30)?.body);
            let raw = entry.take(0x02)?.body;
            if raw.len() > 20 {
                return Err(Error::Certificate);
            }
            let serial = positive_integer(raw)?;
            if serials.contains(&serial) {
                return Err(Error::Certificate);
            }
            serials.push(serial);
            entry.date()?;
            entry.done()?;
        }
    }
    let mut explicit = Der(tbs.take(0xa0)?.body);
    tbs.done()?;
    let mut extensions = Der(explicit.take(0x30)?.body);
    explicit.done()?;
    let mut number = None;
    let mut aki = None;
    for _ in 0..2 {
        let mut extension = Der(extensions.take(0x30)?.body);
        let oid = extension.take(0x06)?.body;
        // Exactly two known, noncritical extensions. Critical/unknown/delta/
        // indirect scope and entry extensions cannot become a complete CRL.
        let value = extension.take(0x04)?.body;
        extension.done()?;
        let mut value = Der(value);
        match oid {
            b"\x55\x1d\x14" if number.is_none() => {
                let raw = value.take(0x02)?.body;
                if raw.len() > 4 {
                    return Err(Error::Certificate);
                }
                let bytes = positive_integer(raw)?;
                let generation = bytes
                    .iter()
                    .fold(0u64, |value, byte| value * 256 + u64::from(*byte));
                if !(1..=MAX_GENERATION).contains(&generation) {
                    return Err(Error::Certificate);
                }
                number = Some(generation);
            }
            b"\x55\x1d\x23" if aki.is_none() => {
                let mut fields = Der(value.take(0x30)?.body);
                let key = fields.take(0x80)?.body;
                fields.done()?;
                if key.len() != 20 {
                    return Err(Error::Certificate);
                }
                aki = Some(key);
            }
            _ => return Err(Error::Certificate),
        }
        value.done()?;
    }
    extensions.done()?;
    outer.algorithm()?;
    let signature = outer.take(0x03)?.body;
    outer.done()?;
    if !(9..=73).contains(&signature.len()) || signature[0] != 0 {
        return Err(Error::Certificate);
    }
    Ok(Preflight {
        tbs: signed.full,
        issuer,
        signature: &signature[1..],
        aki: aki.ok_or(Error::Certificate)?,
        generation: number.ok_or(Error::Certificate)?,
        serials,
    })
}
