//! Test-only synchronous SDK binding. The native context is private and opaque.
use firmware_image::{Crypto, SIGNATURE_BYTES};
use std::alloc::{alloc_zeroed, dealloc, Layout};
use std::ffi::{c_int, c_uint, c_void};
use std::ptr::NonNull;

unsafe extern "C" {
    fn nxrs_admission_rsa_size() -> usize;
    fn nxrs_admission_rsa_align() -> usize;
    fn nxrs_admission_sha256_id() -> c_int;
    fn mbedtls_rsa_init(context: *mut c_void);
    fn mbedtls_rsa_free(context: *mut c_void);
    fn mbedtls_rsa_import_raw(
        context: *mut c_void,
        n: *const u8,
        nl: usize,
        p: *const u8,
        pl: usize,
        q: *const u8,
        ql: usize,
        d: *const u8,
        dl: usize,
        e: *const u8,
        el: usize,
    ) -> c_int;
    fn mbedtls_rsa_complete(context: *mut c_void) -> c_int;
    fn mbedtls_rsa_check_pubkey(context: *const c_void) -> c_int;
    fn mbedtls_rsa_get_len(context: *const c_void) -> usize;
    fn mbedtls_rsa_rsassa_pss_verify_ext(
        context: *mut c_void,
        md: c_int,
        length: c_uint,
        digest: *const u8,
        mgf_md: c_int,
        salt_length: c_int,
        signature: *const u8,
    ) -> c_int;
    fn mbedtls_sha256(input: *const u8, length: usize, output: *mut u8, is224: c_int) -> c_int;
}

#[derive(Debug, PartialEq, Eq)]
pub struct NativeError(pub i32);

pub struct Native {
    context: NonNull<c_void>,
    allocation: Layout,
    sha256_id: c_int,
}

impl Native {
    pub fn new(modulus: &[u8; SIGNATURE_BYTES]) -> Result<Self, NativeError> {
        // SAFETY: getters use the same target headers/configuration as the SDK archive.
        let (size, align, sha256_id) = unsafe {
            (
                nxrs_admission_rsa_size(),
                nxrs_admission_rsa_align(),
                nxrs_admission_sha256_id(),
            )
        };
        if size == 0 || size > 65536 || align == 0 || align > 4096 {
            return Err(NativeError(-1));
        }
        let allocation = Layout::from_size_align(size, align).map_err(|_| NativeError(-1))?;
        // SAFETY: valid bounded layout; ownership immediately enters this RAII guard.
        let context =
            NonNull::new(unsafe { alloc_zeroed(allocation) }.cast()).ok_or(NativeError(-1))?;
        // SAFETY: exclusive SDK-sized/aligned allocation, initialization is synchronous.
        unsafe { mbedtls_rsa_init(context.as_ptr()) };
        let native = Self {
            context,
            allocation,
            sha256_id,
        };
        let empty = std::ptr::null();
        let exponent = [1, 0, 1];
        // SAFETY: arrays outlive synchronous import; absent private components have zero length.
        checked(unsafe {
            mbedtls_rsa_import_raw(
                native.context.as_ptr(),
                modulus.as_ptr(),
                modulus.len(),
                empty,
                0,
                empty,
                0,
                empty,
                0,
                exponent.as_ptr(),
                exponent.len(),
            )
        })?;
        // SAFETY: exclusive initialized context; no SDK callbacks or asynchronous ownership.
        checked(unsafe { mbedtls_rsa_complete(native.context.as_ptr()) })?;
        checked(unsafe { mbedtls_rsa_check_pubkey(native.context.as_ptr()) })?;
        if unsafe { mbedtls_rsa_get_len(native.context.as_ptr()) } != SIGNATURE_BYTES {
            return Err(NativeError(-1));
        }
        Ok(native)
    }
}

fn checked(code: c_int) -> Result<(), NativeError> {
    if code == 0 {
        Ok(())
    } else {
        Err(NativeError(code))
    }
}

impl Crypto for Native {
    type Error = NativeError;
    fn sha256(&mut self, message: &[u8]) -> Result<[u8; 32], NativeError> {
        let mut output = [0; 32];
        // SAFETY: input and fixed output remain valid for this synchronous SDK call.
        checked(unsafe {
            mbedtls_sha256(message.as_ptr(), message.len(), output.as_mut_ptr(), 0)
        })?;
        Ok(output)
    }
    fn verify_rsa3072_pss_sha256(
        &mut self,
        digest: &[u8; 32],
        signature: &[u8; SIGNATURE_BYTES],
    ) -> Result<(), NativeError> {
        // SAFETY: exclusive initialized context, fixed validated digest/signature sizes.
        checked(unsafe {
            mbedtls_rsa_rsassa_pss_verify_ext(
                self.context.as_ptr(),
                self.sha256_id,
                32,
                digest.as_ptr(),
                self.sha256_id,
                32,
                signature.as_ptr(),
            )
        })
    }
}

impl Drop for Native {
    fn drop(&mut self) {
        // SAFETY: no callbacks/other owners exist. SDK frees internal allocations before
        // Rust releases its context using the exact original allocation layout.
        unsafe {
            mbedtls_rsa_free(self.context.as_ptr());
            dealloc(self.context.as_ptr().cast(), self.allocation);
        }
    }
}
