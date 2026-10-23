//! Returned-certificate binding and in-memory revocation admission. Protected
//! epochs, trusted time acquisition and TLS remain separate gates.
use super::*;
use std::slice;

mod revocation;
pub use revocation::{
    CurrentCertificateBinding, RevocationState, TlsCancellation, TlsError, TlsPolicy, TlsProgress,
    TlsServerSession, TlsVersion,
};

thread_local! { static TRUSTED_TIME: Cell<Option<i64>> = const { Cell::new(None) }; }
struct TimeInvocation;
impl Drop for TimeInvocation {
    fn drop(&mut self) {
        TRUSTED_TIME.with(|time| time.set(None));
    }
}
/// Actual SDK time callback. A foreign, late or unscoped call has no trusted UTC.
#[no_mangle]
pub extern "C" fn nxrs_di_trusted_utc() -> i64 {
    TRUSTED_TIME.with(|time| time.get().unwrap_or(-1))
}

use std::time::{Duration, Instant};

/// Independently provisioned camera scope and issuer certificate fingerprint.
/// `trusted_unix_seconds` must be a fresh, trusted UTC observation. This API
/// cannot establish clock trust, a protected generation floor or revocation.
pub struct CameraCertificatePolicy<'a> {
    pub issuer_sha256: [u8; 32],
    pub site_id: &'a str,
    pub camera_id: &'a str,
    pub server_ip: [u8; 4],
    pub trusted_unix_seconds: u64,
}

/// A certificate bound to this live key, pinned issuer, scope and time window.
/// It borrows the key owner and never exports its private key. This is not a TLS
/// authorization token: fresh revocation and protected-generation checks are
/// still required. The observation expires within five minutes, even when the
/// certificate lasts longer; a later connection must repeat admission policy.
pub struct CertificateBinding<'key, 'entropy> {
    owner: &'key Identity<'entropy>,
    der: Vec<u8>,
    serial: Vec<u8>,
    observed: Instant,
    valid_for: Duration,
    valid_until_unix: u64,
}
impl CertificateBinding<'_, '_> {
    pub fn remaining_validity(&self) -> Result<Duration, Error> {
        if self.owner.failed() {
            return Err(Error::Poisoned);
        }
        self.valid_for
            .checked_sub(self.observed.elapsed())
            .filter(|remaining| !remaining.is_zero())
            .ok_or(Error::Expired)
    }
    pub fn certificate_der(&self) -> Result<&[u8], Error> {
        self.remaining_validity()?;
        Ok(&self.der)
    }
}

// Camera and ingest leaves intentionally share this enrollment protocol.
// Callers still own their distinct identity policy and proof-of-key checks.
struct LeafPolicy<'a> {
    issuer_sha256: [u8; 32],
    uri: &'a str,
    ip: Option<[u8; 4]>,
    eku: &'static [u8],
    trusted_unix_seconds: u64,
}
struct VerifiedLeaf {
    certificate: Opaque,
    serial: Vec<u8>,
    observed: Instant,
    valid_for: Duration,
    valid_until_unix: u64,
}

// All field offsets and sizes are asserted against the actual native headers.
#[repr(C)]
struct SdkTime {
    year: c_int,
    month: c_int,
    day: c_int,
    hour: c_int,
    minute: c_int,
    second: c_int,
}
#[repr(C)]
struct Profile {
    hashes: u32,
    keys: u32,
    curves: u32,
    minimum_rsa_bits: u32,
}
type VerifyCallback = unsafe extern "C" fn(*mut c_void, *mut c_void, c_int, *mut u32) -> c_int;
unsafe extern "C" {
    fn nxrs_di_crt_serial(p: *const c_void) -> *const c_void;
    fn nxrs_di_buf_data(p: *const c_void) -> *const u8;
    fn nxrs_di_buf_length(p: *const c_void) -> usize;
    fn nxrs_di_crt_size() -> usize;
    fn nxrs_di_crt_align() -> usize;
    fn mbedtls_x509_crt_init(p: *mut c_void);
    fn mbedtls_x509_crt_free(p: *mut c_void);
    fn mbedtls_x509_crt_parse_der(p: *mut c_void, input: *const u8, length: usize) -> c_int;
    fn mbedtls_x509_crt_verify_with_profile(
        leaf: *mut c_void,
        root: *mut c_void,
        crl: *mut c_void,
        profile: *const Profile,
        name: *const c_char,
        flags: *mut u32,
        callback: Option<VerifyCallback>,
        context: *mut c_void,
    ) -> c_int;
    fn nxrs_di_crt_raw_length(p: *const c_void) -> usize;
    fn nxrs_di_crt_next(p: *const c_void) -> *const c_void;
    fn nxrs_di_crt_from(p: *const c_void) -> *const SdkTime;
    fn nxrs_di_crt_to(p: *const c_void) -> *const SdkTime;
    fn nxrs_di_crt_pk(p: *mut c_void) -> *mut c_void;
    fn nxrs_di_crt_ca(p: *const c_void) -> c_int;
    fn nxrs_di_crt_path_length(p: *const c_void) -> c_int;
    fn nxrs_di_crt_usage(p: *const c_void) -> u32;
    fn nxrs_di_crt_extensions(p: *const c_void) -> u32;
    fn nxrs_di_crt_sans(p: *const c_void) -> *const c_void;
    fn nxrs_di_crt_ekus(p: *const c_void) -> *const c_void;
    fn nxrs_di_sequence_next(p: *const c_void) -> *const c_void;
    fn nxrs_di_sequence_data(p: *const c_void) -> *const u8;
    fn nxrs_di_sequence_length(p: *const c_void) -> usize;
    fn nxrs_di_sequence_tag(p: *const c_void) -> c_int;
    fn nxrs_di_usage_signature() -> u32;
    fn nxrs_di_extension_constraints() -> u32;
    fn nxrs_di_extension_usage() -> u32;
    fn nxrs_di_extension_eku() -> u32;
    fn nxrs_di_extension_san() -> u32;
    fn nxrs_di_san_uri_tag() -> c_int;
    fn nxrs_di_san_ip_tag() -> c_int;
    fn nxrs_di_profile_sha256() -> u32;
    fn nxrs_di_profile_ec_keys() -> u32;
    fn nxrs_di_profile_p256() -> u32;
    fn nxrs_di_psa_sha256() -> u32;
    fn psa_hash_compute(
        algorithm: u32,
        input: *const u8,
        size: usize,
        output: *mut u8,
        capacity: usize,
        length: *mut usize,
    ) -> c_int;
    fn mbedtls_pk_write_pubkey_der(pk: *mut c_void, output: *mut u8, capacity: usize) -> c_int;
}

// A private view of an actual, live SDK-owned buffer. Callers bound the
// corresponding native object lifetime; no view escapes its synchronous use.
unsafe fn buffer<'a>(p: *const c_void, maximum: usize) -> Result<&'a [u8], Error> {
    // SAFETY: caller supplies the native buffer field from a live SDK object.
    let (data, length) = unsafe { (nxrs_di_buf_data(p), nxrs_di_buf_length(p)) };
    if data.is_null() || length == 0 || length > maximum {
        return Err(Error::Certificate);
    }
    // SAFETY: native parser owns this nonempty, bounded field for this call.
    Ok(unsafe { slice::from_raw_parts(data, length) })
}
fn positive_integer(bytes: &[u8]) -> Result<&[u8], Error> {
    if bytes.is_empty()
        || bytes[0] & 0x80 != 0
        || (bytes.len() > 1 && bytes[0] == 0 && bytes[1] & 0x80 == 0)
        || bytes.iter().all(|byte| *byte == 0)
    {
        return Err(Error::Certificate);
    }
    Ok(if bytes[0] == 0 { &bytes[1..] } else { bytes })
}

fn identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}
fn unix(time: &SdkTime) -> Result<u64, Error> {
    let y = time.year;
    let leap = y % 4 == 0 && (y % 100 != 0 || y % 400 == 0);
    let months = [
        31,
        if leap { 29 } else { 28 },
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ];
    if !(1970..=9999).contains(&y)
        || !(1..=12).contains(&time.month)
        || !(1..=months[(time.month - 1) as usize]).contains(&time.day)
        || !(0..24).contains(&time.hour)
        || !(0..60).contains(&time.minute)
        || !(0..60).contains(&time.second)
    {
        return Err(Error::Certificate);
    }
    let prior = |year: i32| {
        let n = (year - 1) as u64;
        365 * n + n / 4 - n / 100 + n / 400
    };
    let days = prior(y) - prior(1970)
        + months[..(time.month - 1) as usize]
            .iter()
            .map(|v| *v as u64)
            .sum::<u64>()
        + (time.day - 1) as u64;
    Ok(days * 86400 + time.hour as u64 * 3600 + time.minute as u64 * 60 + time.second as u64)
}

// Pointers are internal, borrowed fields of a successfully parsed, live SDK
// object. Each traversal and view is bounded before copying into Rust storage.
fn sequence(mut p: *const c_void) -> Result<Vec<(c_int, Vec<u8>)>, Error> {
    let mut result = Vec::new();
    result.try_reserve_exact(3).map_err(|_| Error::Capacity)?;
    while !p.is_null() {
        if result.len() == 3 {
            return Err(Error::Certificate);
        }
        // SAFETY: p starts at an actual SDK sequence and follows its native next.
        let (data, length, tag, next) = unsafe {
            (
                nxrs_di_sequence_data(p),
                nxrs_di_sequence_length(p),
                nxrs_di_sequence_tag(p),
                nxrs_di_sequence_next(p),
            )
        };
        if data.is_null() || length == 0 || length > 256 {
            return Err(Error::Certificate);
        }
        // SAFETY: the SDK-owned view remains live and immutable during the copy.
        result.push((
            tag,
            try_bytes(unsafe { slice::from_raw_parts(data, length) })?,
        ));
        p = next;
    }
    Ok(result)
}

impl<'entropy> Identity<'entropy> {
    fn certificate_sdk(&self, operation: impl FnOnce() -> c_int) -> Result<(), Error> {
        self.sdk(operation).map_err(|error| {
            if error == Error::Crypto {
                Error::Certificate
            } else {
                error
            }
        })
    }
    fn parse_certificate(&self, der: &[u8]) -> Result<Opaque, Error> {
        if der.is_empty() || der.len() > 2048 {
            return Err(Error::Capacity);
        }
        // SAFETY: exact SDK size/alignment/init/free operations, stable allocation.
        let certificate = unsafe {
            Opaque::new(
                nxrs_di_crt_size(),
                nxrs_di_crt_align(),
                mbedtls_x509_crt_init,
                mbedtls_x509_crt_free,
            )?
        };
        // SAFETY: DER is borrowed for the synchronous parser, which copies input.
        self.certificate_sdk(|| unsafe {
            mbedtls_x509_crt_parse_der(certificate.pointer(), der.as_ptr(), der.len())
        })?;
        // SAFETY: actual fields of the successfully parsed SDK object. Refuse
        // additional certificates or bytes not covered by its parsed DER object.
        if unsafe {
            nxrs_di_crt_raw_length(certificate.pointer()) != der.len()
                || !nxrs_di_crt_next(certificate.pointer()).is_null()
        } {
            return Err(Error::Certificate);
        }
        Ok(certificate)
    }
    fn public_der(&self, pk: *mut c_void) -> Result<Vec<u8>, Error> {
        let mut output = [0u8; 256];
        let mut length = 0;
        // SAFETY: pk is an initialized live SDK public/borrowed-key context;
        // the SDK writes backwards into this exclusive bounded output buffer.
        self.certificate_sdk(|| unsafe {
            length = mbedtls_pk_write_pubkey_der(pk, output.as_mut_ptr(), output.len());
            if length > 0 {
                0
            } else {
                -1
            }
        })?;
        let length = usize::try_from(length).map_err(|_| Error::Certificate)?;
        if length > output.len() {
            return Err(Error::Certificate);
        }
        try_bytes(&output[output.len() - length..])
    }
    /// Verify one DER leaf returned by the offline issuer against this key,
    /// pinned DER root, camera URI/IP, P-256/SHA-256 chain, EKU and trusted UTC.
    /// Invalid inputs leave the key available for a corrected response; entropy
    /// or unexpected-callback failures still close the owner permanently.
    ///
    /// This binding does not check revocation or a protected generation floor,
    /// establish trusted time, authorize a TLS peer or persist a credential.
    pub fn bind_certificate<'key>(
        &'key self,
        leaf_der: &[u8],
        root_der: &[u8],
        policy: CameraCertificatePolicy<'_>,
    ) -> Result<CertificateBinding<'key, 'entropy>, Error> {
        if !identifier(policy.site_id)
            || !identifier(policy.camera_id)
            || !(std::net::Ipv4Addr::from(policy.server_ip).is_private()
                || std::net::Ipv4Addr::from(policy.server_ip).is_loopback())
        {
            return Err(Error::InvalidInput);
        }
        let uri = try_text(&["urn:nxrs:camera:", policy.site_id, ":", policy.camera_id])?;
        let verified = self.verify_leaf(
            leaf_der,
            root_der,
            LeafPolicy {
                issuer_sha256: policy.issuer_sha256,
                uri: &uri,
                ip: Some(policy.server_ip),
                eku: b"\x2b\x06\x01\x05\x05\x07\x03\x01",
                trusted_unix_seconds: policy.trusted_unix_seconds,
            },
        )?;
        // SAFETY: the verified parsed leaf and this owner's key remain live.
        let public = self.public_der(unsafe { nxrs_di_crt_pk(verified.certificate.pointer()) })?;
        let owned = self.public_der(self.pk.as_ref().ok_or(Error::Poisoned)?.pointer())?;
        if public != owned {
            return Err(Error::Certificate);
        }
        let binding = CertificateBinding {
            owner: self,
            der: try_bytes(leaf_der)?,
            serial: verified.serial,
            observed: verified.observed,
            valid_for: verified.valid_for,
            valid_until_unix: verified.valid_until_unix,
        };
        binding.remaining_validity()?;
        Ok(binding)
    }
    fn verify_leaf(
        &self,
        leaf_der: &[u8],
        root_der: &[u8],
        policy: LeafPolicy<'_>,
    ) -> Result<VerifiedLeaf, Error> {
        let observed = Instant::now();
        if policy.trusted_unix_seconds == 0
            || policy.trusted_unix_seconds > 253402300799
            || root_der.is_empty()
            || root_der.len() > 2048
        {
            return Err(Error::InvalidInput);
        }
        let mut fingerprint = [0; 32];
        let mut length = 0;
        // SAFETY: actual PSA algorithm constant and exclusive SHA-256 output.
        self.certificate_sdk(|| unsafe {
            psa_hash_compute(
                nxrs_di_psa_sha256(),
                root_der.as_ptr(),
                root_der.len(),
                fingerprint.as_mut_ptr(),
                fingerprint.len(),
                &mut length,
            )
        })?;
        if length != 32 || fingerprint != policy.issuer_sha256 {
            return Err(Error::Certificate);
        }
        let root = self.parse_certificate(root_der)?;
        let leaf = self.parse_certificate(leaf_der)?;
        let r = root.pointer();
        let l = leaf.pointer();
        // SAFETY: exact live SDK parsed-object fields and native flag constants.
        let (root_ca, root_path, leaf_ca, usage, extensions, required, profile) = unsafe {
            (
                nxrs_di_crt_ca(r),
                nxrs_di_crt_path_length(r),
                nxrs_di_crt_ca(l),
                nxrs_di_crt_usage(l),
                nxrs_di_crt_extensions(l),
                nxrs_di_extension_constraints()
                    | nxrs_di_extension_usage()
                    | nxrs_di_extension_eku()
                    | nxrs_di_extension_san(),
                Profile {
                    hashes: nxrs_di_profile_sha256(),
                    keys: nxrs_di_profile_ec_keys(),
                    curves: nxrs_di_profile_p256(),
                    minimum_rsa_bits: u32::MAX,
                },
            )
        };
        if root_ca != 1 || root_path != 1 || leaf_ca != 0 || extensions & required != required
            // SAFETY: header constant only.
            || usage != unsafe {nxrs_di_usage_signature()}
        {
            return Err(Error::Certificate);
        }
        let mut expires = u64::MAX;
        for certificate in [r, l] {
            // SAFETY: live native fields with the six-int ABI asserted in C.
            let (from, to) = unsafe {
                (
                    unix(&*nxrs_di_crt_from(certificate))?,
                    unix(&*nxrs_di_crt_to(certificate))?,
                )
            };
            if from > policy.trusted_unix_seconds || to <= policy.trusted_unix_seconds || to <= from
            {
                return Err(Error::Certificate);
            }
            if certificate == l && to - from > 30 * 86400 + 300 {
                return Err(Error::Certificate);
            }
            expires = expires.min(to);
        }
        // SAFETY: live successfully parsed leaf throughout field inspection.
        let (serial, _) = unsafe { inspect_leaf(l, &policy) }?;
        let mut flags = u32::MAX;
        TRUSTED_TIME.with(|time| {
            if time.get().is_some() {
                return Err(Error::Busy);
            }
            time.set(Some(policy.trusted_unix_seconds as i64));
            Ok(())
        })?;
        let time_invocation = TimeInvocation;
        // SAFETY: live leaf/root, fixed profile ABI, exclusive flags; no callback
        // can waive a verification flag. URI/IP are checked above in Rust.
        self.certificate_sdk(|| unsafe {
            mbedtls_x509_crt_verify_with_profile(
                l,
                r,
                ptr::null_mut(),
                &profile,
                ptr::null(),
                &mut flags,
                None,
                ptr::null_mut(),
            )
        })?;
        drop(time_invocation);
        if flags != 0 {
            return Err(Error::Certificate);
        }
        let valid_for = Duration::from_secs((expires - policy.trusted_unix_seconds).min(300));
        if self.failed() {
            return Err(Error::Poisoned);
        }
        if observed.elapsed() >= valid_for {
            return Err(Error::Expired);
        }
        Ok(VerifiedLeaf {
            certificate: leaf,
            serial,
            observed,
            valid_for,
            valid_until_unix: expires,
        })
    }
}

// SAFETY: caller keeps one successfully parsed actual SDK leaf alive for this
// inspection. Only bounded borrowed field views are copied; no crypto call.
unsafe fn inspect_leaf(l: *const c_void, policy: &LeafPolicy<'_>) -> Result<(Vec<u8>, u64), Error> {
    // SAFETY: caller's live parsed leaf; native constants and field views only.
    let (ca, usage, extensions, required, from, to) = unsafe {
        (
            nxrs_di_crt_ca(l),
            nxrs_di_crt_usage(l),
            nxrs_di_crt_extensions(l),
            nxrs_di_extension_constraints()
                | nxrs_di_extension_usage()
                | nxrs_di_extension_eku()
                | nxrs_di_extension_san(),
            unix(&*nxrs_di_crt_from(l))?,
            unix(&*nxrs_di_crt_to(l))?,
        )
    };
    if ca != 0 || extensions & required != required
        // SAFETY: native header constant only.
        || usage != unsafe { nxrs_di_usage_signature() }
        || from > policy.trusted_unix_seconds || to <= policy.trusted_unix_seconds
        || to <= from || to - from > 30 * 86400 + 300
    {
        return Err(Error::Certificate);
    }
    // SAFETY: live native sequence fields and header tag constants.
    let (sans, ekus, uri_tag, ip_tag) = unsafe {
        (
            sequence(nxrs_di_crt_sans(l))?,
            sequence(nxrs_di_crt_ekus(l))?,
            nxrs_di_san_uri_tag(),
            nxrs_di_san_ip_tag(),
        )
    };
    if sans.len() != 1 + usize::from(policy.ip.is_some())
        || sans
            .iter()
            .filter(|(tag, bytes)| *tag == uri_tag && bytes == policy.uri.as_bytes())
            .count()
            != 1
        || policy.ip.is_some_and(|ip| {
            sans.iter()
                .filter(|(tag, bytes)| *tag == ip_tag && bytes == &ip)
                .count()
                != 1
        })
        || ekus.len() != 1
        || ekus[0].1 != policy.eku
    {
        return Err(Error::Certificate);
    }
    // SAFETY: same parsed leaf remains live through the bounded serial copy.
    let serial = try_bytes(positive_integer(unsafe {
        buffer(nxrs_di_crt_serial(l), 20)?
    })?)?;
    Ok((serial, to))
}
