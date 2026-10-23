//! Volatile device enrollment key and CSR over the pinned PSA/X.509 SDK.
//!
//! The platform must supply a cryptographic entropy source with enforced hardware
//! ownership. There is no default source, file seed, private-key export or storage
//! fallback. This library does not establish physical entropy quality, hardware
//! key isolation, persistent custody, certificate authorization or device TLS.
#![deny(unsafe_op_in_unsafe_fn)]

use std::alloc::{alloc_zeroed, dealloc, Layout};
use std::cell::{Cell, RefCell};
use std::ffi::{c_char, c_int, c_void, CString};
use std::marker::PhantomData;
use std::ptr::{self, NonNull};
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

/// A successful fill must supply the full buffer with cryptographic entropy.
/// Hardware providers must own the documented entropy source, bound acquisition
/// and fill, and return an error on unavailable/unqualified/partial output.
/// Implementations must not call back into this SDK or panic.
pub trait Entropy {
    fn fill(&mut self, output: &mut [u8]) -> Result<(), EntropyUnavailable>;
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EntropyUnavailable;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Busy,
    Entropy,
    Poisoned,
    InvalidInput,
    Capacity,
    Crypto,
}

unsafe extern "C" {
    fn nxrs_di_attributes_size() -> usize;
    fn nxrs_di_attributes_align() -> usize;
    fn nxrs_di_attributes_init(p: *mut c_void);
    fn nxrs_di_set_type(p: *mut c_void, value: u16);
    fn nxrs_di_set_bits(p: *mut c_void, value: usize);
    fn nxrs_di_set_usage(p: *mut c_void, value: u32);
    fn nxrs_di_set_algorithm(p: *mut c_void, value: u32);
    fn nxrs_di_set_lifetime(p: *mut c_void, value: u32);
    fn nxrs_di_p256_type() -> u16;
    fn nxrs_di_sign_hash_usage() -> u32;
    fn nxrs_di_ecdsa_sha256() -> u32;
    fn nxrs_di_volatile_lifetime() -> u32;
    fn nxrs_di_sha256() -> c_int;
    fn nxrs_di_entropy_error() -> c_int;
    fn nxrs_di_pk_size() -> usize;
    fn nxrs_di_pk_align() -> usize;
    fn nxrs_di_csr_size() -> usize;
    fn nxrs_di_csr_align() -> usize;
    fn psa_crypto_init() -> c_int;
    fn mbedtls_psa_crypto_free();
    fn psa_generate_key(attributes: *const c_void, key: *mut u32) -> c_int;
    fn psa_destroy_key(key: u32) -> c_int;
    fn psa_random_reseed(personalization: *const u8, size: usize) -> c_int;
    fn psa_reset_key_attributes(attributes: *mut c_void);
    fn mbedtls_pk_init(p: *mut c_void);
    fn mbedtls_pk_free(p: *mut c_void);
    fn mbedtls_pk_wrap_psa(p: *mut c_void, key: u32) -> c_int;
    fn mbedtls_x509write_csr_init(p: *mut c_void);
    fn mbedtls_x509write_csr_free(p: *mut c_void);
    fn mbedtls_x509write_csr_set_key(p: *mut c_void, key: *mut c_void);
    fn mbedtls_x509write_csr_set_md_alg(p: *mut c_void, algorithm: c_int);
    fn mbedtls_x509write_csr_set_subject_name(p: *mut c_void, subject: *const c_char) -> c_int;
    fn mbedtls_x509write_csr_pem(p: *mut c_void, output: *mut u8, size: usize) -> c_int;
}

struct Opaque {
    pointer: NonNull<u8>,
    layout: Layout,
    retire: unsafe extern "C" fn(*mut c_void),
}
impl Opaque {
    unsafe fn new(
        size: usize,
        align: usize,
        initialize: unsafe extern "C" fn(*mut c_void),
        retire: unsafe extern "C" fn(*mut c_void),
    ) -> Result<Self, Error> {
        if size == 0 || size > 16_384 || align == 0 || align > 64 {
            return Err(Error::Capacity);
        }
        let layout = Layout::from_size_align(size, align).map_err(|_| Error::Capacity)?;
        // SAFETY: validated layout; allocation stays stable until SDK retirement.
        let pointer = NonNull::new(unsafe { alloc_zeroed(layout) }).ok_or(Error::Capacity)?;
        // SAFETY: caller pairs actual SDK size/alignment and init/free operations.
        unsafe { initialize(pointer.as_ptr().cast()) };
        Ok(Self {
            pointer,
            layout,
            retire,
        })
    }
    fn pointer(&self) -> *mut c_void {
        self.pointer.as_ptr().cast()
    }
}
impl Drop for Opaque {
    fn drop(&mut self) {
        // SAFETY: sole stable allocation, retired before its storage is freed.
        unsafe {
            (self.retire)(self.pointer());
            dealloc(self.pointer.as_ptr(), self.layout);
        }
    }
}

struct Callback<'a> {
    source: RefCell<Box<dyn Entropy + 'a>>,
    failed: AtomicBool,
    calls: AtomicUsize,
}
// The SDK driver callback has no userdata parameter. A call-scoped thread-local
// pointer borrows the owner's stable Box only during a synchronous SDK operation.
// It is cleared before the operation returns. Foreign/late callbacks cannot
// dereference owner storage. One lease exclusively owns the library's PSA state.
static OWNED: AtomicBool = AtomicBool::new(false);
static UNEXPECTED_CALLBACK: AtomicBool = AtomicBool::new(false);
thread_local! { static INVOCATION: Cell<*mut c_void> = const { Cell::new(ptr::null_mut()) }; }
struct Lease;
impl Drop for Lease {
    fn drop(&mut self) {
        OWNED.store(false, Ordering::Release);
    }
}
struct Invocation;
impl Drop for Invocation {
    fn drop(&mut self) {
        INVOCATION.with(|slot| slot.set(ptr::null_mut()));
    }
}

/// Actual TF-PSA-Crypto driver callback; not a Rust-to-Rust service interface.
///
/// # Safety
/// The SDK supplies writable, disjoint output/estimate buffers for this call.
#[no_mangle]
pub unsafe extern "C" fn mbedtls_platform_get_entropy(
    flags: u32,
    estimate_bits: *mut usize,
    output: *mut u8,
    output_size: usize,
) -> c_int {
    // SAFETY: a native header constant, independent of initialized PSA state.
    let failure = unsafe { nxrs_di_entropy_error() };
    if !estimate_bits.is_null() {
        unsafe { *estimate_bits = 0 };
    }
    if estimate_bits.is_null() || output.is_null() || output_size == 0 || output_size > 256 {
        return failure;
    }
    // SAFETY: the actual SDK callback ABI supplies these bounded writable regions.
    let bytes = unsafe { std::slice::from_raw_parts_mut(output, output_size) };
    bytes.fill(0);
    unsafe { *estimate_bits = 0 };
    let pointer = INVOCATION.with(Cell::get);
    if pointer.is_null() {
        UNEXPECTED_CALLBACK.store(true, Ordering::Release);
        return failure;
    }
    // SAFETY: the active synchronous call borrows the owner and its stable Box.
    let callback = unsafe { &*pointer.cast::<Callback<'_>>() };
    if flags != 0 || callback.failed.load(Ordering::Acquire) {
        callback.failed.store(true, Ordering::Release);
        return failure;
    }
    let Ok(mut source) = callback.source.try_borrow_mut() else {
        callback.failed.store(true, Ordering::Release);
        return failure;
    };
    callback.calls.fetch_add(1, Ordering::Relaxed);
    if source.fill(bytes).is_err() {
        bytes.fill(0);
        callback.failed.store(true, Ordering::Release);
        return failure;
    }
    // The Entropy contract, not a statistical test, supplies this assertion.
    unsafe { *estimate_bits = output_size * 8 };
    0
}

/// A single volatile, non-exportable P-256 signing key. PSA slot isolation is a
/// software policy, not protection against privileged memory access or reset.
/// There is no Clone/Debug implementation and no private-key export method.
pub struct Identity<'a> {
    callback: Box<Callback<'a>>,
    key: u32,
    pk: Option<Opaque>,
    _single_thread: PhantomData<Rc<()>>,
    // Declared last: source destruction must finish before another SDK owner.
    _lease: Lease,
}
impl<'a> Identity<'a> {
    pub fn generate(source: impl Entropy + 'a) -> Result<Self, Error> {
        let callback = Box::new(Callback {
            source: RefCell::new(Box::new(source)),
            failed: AtomicBool::new(false),
            calls: AtomicUsize::new(0),
        });
        OWNED
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| Error::Busy)?;
        UNEXPECTED_CALLBACK.store(false, Ordering::Release);
        let mut identity = Self {
            callback,
            key: 0,
            pk: None,
            _single_thread: PhantomData,
            _lease: Lease,
        };
        // SAFETY: sole SDK owner; the stable callback source already exists.
        identity.sdk(|| unsafe { psa_crypto_init() })?;
        // SAFETY: all layout/init/retire operations come from the same SDK build.
        let attributes = unsafe {
            Opaque::new(
                nxrs_di_attributes_size(),
                nxrs_di_attributes_align(),
                nxrs_di_attributes_init,
                psa_reset_key_attributes,
            )?
        };
        // SAFETY: native setters operate on the initialized actual SDK attributes.
        unsafe {
            nxrs_di_set_type(attributes.pointer(), nxrs_di_p256_type());
            nxrs_di_set_bits(attributes.pointer(), 256);
            nxrs_di_set_usage(attributes.pointer(), nxrs_di_sign_hash_usage());
            nxrs_di_set_algorithm(attributes.pointer(), nxrs_di_ecdsa_sha256());
            nxrs_di_set_lifetime(attributes.pointer(), nxrs_di_volatile_lifetime());
        }
        // SAFETY: initialized attributes and exclusive key output. Any created
        // key is adopted immediately, including an SDK error/fault path.
        let mut key = 0;
        let result = identity.sdk(|| unsafe { psa_generate_key(attributes.pointer(), &mut key) });
        identity.key = key;
        result?;
        if identity.key == 0 {
            return Err(Error::Crypto);
        }
        let pk = unsafe {
            Opaque::new(
                nxrs_di_pk_size(),
                nxrs_di_pk_align(),
                mbedtls_pk_init,
                mbedtls_pk_free,
            )?
        };
        identity.sdk(|| unsafe { mbedtls_pk_wrap_psa(pk.pointer(), identity.key) })?;
        identity.pk = Some(pk);
        Ok(identity)
    }

    fn failed(&self) -> bool {
        self.callback.failed.load(Ordering::Acquire) || UNEXPECTED_CALLBACK.load(Ordering::Acquire)
    }

    fn sdk(&self, operation: impl FnOnce() -> c_int) -> Result<(), Error> {
        if self.failed() {
            return Err(Error::Poisoned);
        }
        INVOCATION.with(|slot| {
            if !slot.get().is_null() {
                return Err(Error::Busy);
            }
            // SAFETY: this borrow and Box remain alive until the scope ends.
            slot.set((&*self.callback as *const Callback<'a>).cast_mut().cast());
            Ok(())
        })?;
        let invocation = Invocation;
        let status = operation();
        drop(invocation);
        if self.failed() {
            return Err(Error::Entropy);
        }
        if status != 0 {
            return Err(Error::Crypto);
        }
        Ok(())
    }

    /// Create a public PEM CSR with an advisory CN only. The offline issuer
    /// assigns camera/site/gateway identity from its approved catalog, not this
    /// request. Output must have 512..=4096 bytes; SDK failures clear that buffer.
    pub fn request(&mut self, camera_id: &str, output: &mut [u8]) -> Result<usize, Error> {
        if !(512..=4096).contains(&output.len()) {
            return Err(Error::Capacity);
        }
        output.fill(0);
        if self.failed() {
            return Err(Error::Poisoned);
        }
        if camera_id.is_empty()
            || camera_id.len() > 64
            || !camera_id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
        {
            return Err(Error::InvalidInput);
        }
        let subject = CString::new(format!("CN={camera_id}")).map_err(|_| Error::InvalidInput)?;
        // SAFETY: exact SDK layouts and matching init/free, one stable key owner.
        let csr = unsafe {
            Opaque::new(
                nxrs_di_csr_size(),
                nxrs_di_csr_align(),
                mbedtls_x509write_csr_init,
                mbedtls_x509write_csr_free,
            )?
        };
        let pk = self.pk.as_ref().ok_or(Error::Poisoned)?;
        unsafe {
            mbedtls_x509write_csr_set_key(csr.pointer(), pk.pointer());
            mbedtls_x509write_csr_set_md_alg(csr.pointer(), nxrs_di_sha256());
        }
        self.sdk(|| unsafe {
            mbedtls_x509write_csr_set_subject_name(csr.pointer(), subject.as_ptr())
        })?;
        let result = self.sdk(|| unsafe {
            mbedtls_x509write_csr_pem(csr.pointer(), output.as_mut_ptr(), output.len())
        });
        if let Err(error) = result {
            output.fill(0);
            return Err(error);
        }
        let length = match output.iter().position(|b| *b == 0) {
            Some(length) => length,
            None => {
                output.fill(0);
                return Err(Error::Crypto);
            }
        };
        if length == 0 || !output[..length].ends_with(b"-----END CERTIFICATE REQUEST-----\n") {
            output.fill(0);
            return Err(Error::Crypto);
        }
        Ok(length)
    }

    /// Require fresh entropy, for example after a supported suspend/resume.
    /// The platform must first establish the source's resource lease. A failed
    /// refresh permanently closes this owner even if the SDK retains old DRBG
    /// state. VM/image cloning and physical source qualification are not inferred.
    pub fn refresh_entropy(&mut self) -> Result<(), Error> {
        self.sdk(|| unsafe { psa_random_reseed(ptr::null(), 0) })
    }

    /// Counts only calls to the supplied source; never reveals sample bytes.
    pub fn entropy_calls(&self) -> usize {
        self.callback.calls.load(Ordering::Relaxed)
    }
}
impl Drop for Identity<'_> {
    fn drop(&mut self) {
        // SDK borrowed-key context must retire before destroying its PSA key.
        drop(self.pk.take());
        // SAFETY: all operations and callback execution are synchronous. The
        // sole owner and source remain alive until native PSA/DRBG teardown ends.
        unsafe {
            if self.key != 0 {
                let _ = psa_destroy_key(self.key);
                self.key = 0;
            }
            mbedtls_psa_crypto_free();
        }
    }
}

#[cfg(all(test, nxrs_device_identity_qualification))]
#[path = "../../../tests/device-identity/sdk.rs"]
mod sdk_tests;
