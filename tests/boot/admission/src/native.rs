//! Test-only synchronous SDK binding. The native context is private and opaque.
use firmware_image::{Crypto, IncrementalHash, SIGNATURE_BYTES};
use std::alloc::{alloc_zeroed, dealloc, Layout};
use std::ffi::{c_int, c_uint, c_void};
use std::ptr::NonNull;

unsafe extern "C" {
    fn nxrs_admission_rsa_size() -> usize;
    fn nxrs_admission_rsa_align() -> usize;
    fn nxrs_admission_sha256_id() -> c_int;
    fn nxrs_admission_sha_size() -> usize;
    fn nxrs_admission_sha_align() -> usize;
    fn mbedtls_sha256_init(context: *mut c_void);
    fn mbedtls_sha256_free(context: *mut c_void);
    fn mbedtls_sha256_starts(context: *mut c_void, is224: c_int) -> c_int;
    fn mbedtls_sha256_update(context: *mut c_void, input: *const u8, length: usize) -> c_int;
    fn mbedtls_sha256_finish(context: *mut c_void, output: *mut u8) -> c_int;
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
    fn mbedtls_rsa_export_raw(
        context: *const c_void,
        n: *mut u8,
        nl: usize,
        p: *mut u8,
        pl: usize,
        q: *mut u8,
        ql: usize,
        d: *mut u8,
        dl: usize,
        e: *mut u8,
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
    key_sha256: [u8; 32],
}

// SAFETY: this fixture uses the SDK's ordinary process allocator, no arena,
// threading module, callbacks or borrowed buffers. Moving its exclusive opaque
// context transfers ownership; all SDK access remains synchronous through &mut.
unsafe impl Send for Native {}

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
        let mut native = Self {
            context,
            allocation,
            sha256_id,
            key_sha256: [0; 32],
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
        // Re-export the actual imported public key. No private components exist.
        let mut actual_modulus = [0; SIGNATURE_BYTES];
        let mut actual_exponent = [0; 3];
        let absent = std::ptr::null_mut();
        // SAFETY: initialized public context, exact output buffers, synchronous call.
        checked(unsafe {
            mbedtls_rsa_export_raw(
                native.context.as_ptr(),
                actual_modulus.as_mut_ptr(),
                actual_modulus.len(),
                absent,
                0,
                absent,
                0,
                absent,
                0,
                actual_exponent.as_mut_ptr(),
                actual_exponent.len(),
            )
        })?;
        if actual_modulus != *modulus
            || actual_modulus[0] & 0x80 == 0
            || actual_exponent != exponent
        {
            return Err(NativeError(-1));
        }
        // Canonical PKCS#1 DER for a positive 3072-bit N and E=65537:
        // SEQUENCE { INTEGER (leading sign zero + N), INTEGER E }.
        let mut der = [0; 398];
        der[..9].copy_from_slice(&[0x30, 0x82, 0x01, 0x8a, 0x02, 0x82, 0x01, 0x81, 0]);
        der[9..393].copy_from_slice(&actual_modulus);
        der[393..].copy_from_slice(&[0x02, 0x03, 0x01, 0x00, 0x01]);
        native.key_sha256 = native.sha256(&der)?;
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
    fn key_sha256(&self) -> [u8; 32] {
        self.key_sha256
    }
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

/// Test-only actual SDK incremental SHA provider. No image signature authority.
pub struct NativeSha {
    context: NonNull<c_void>,
    allocation: Layout,
    active: bool,
    failed: bool,
}
impl NativeSha {
    pub fn new() -> Result<Self, NativeError> {
        // SAFETY: these getters compile against the exact SDK archive headers.
        let (size, align) = unsafe { (nxrs_admission_sha_size(), nxrs_admission_sha_align()) };
        if size == 0 || size > 65536 || align == 0 || align > 4096 {
            return Err(NativeError(-1));
        }
        let allocation = Layout::from_size_align(size, align).map_err(|_| NativeError(-1))?;
        // SAFETY: valid SDK-sized/aligned owned allocation; no borrowed SDK state.
        let context =
            NonNull::new(unsafe { alloc_zeroed(allocation) }.cast()).ok_or(NativeError(-1))?;
        // SAFETY: exclusive correctly sized context; synchronous initialization.
        unsafe { mbedtls_sha256_init(context.as_ptr()) };
        Ok(Self {
            context,
            allocation,
            active: false,
            failed: false,
        })
    }
    fn result(&mut self, code: c_int) -> Result<(), NativeError> {
        if code != 0 {
            self.failed = true;
            self.active = false;
        }
        checked(code)
    }
}
impl IncrementalHash for NativeSha {
    type Error = NativeError;
    fn reset(&mut self) -> Result<(), NativeError> {
        if self.failed {
            return Err(NativeError(-1));
        }
        // SAFETY: exclusive initialized context; SDK keeps no borrowed input.
        let code = unsafe { mbedtls_sha256_starts(self.context.as_ptr(), 0) };
        self.result(code)?;
        self.active = true;
        Ok(())
    }
    fn update(&mut self, bytes: &[u8]) -> Result<(), NativeError> {
        if !self.active || self.failed {
            return Err(NativeError(-1));
        }
        // SAFETY: exact input lives through the synchronous SDK call.
        let code =
            unsafe { mbedtls_sha256_update(self.context.as_ptr(), bytes.as_ptr(), bytes.len()) };
        self.result(code)
    }
    fn finish(&mut self) -> Result<[u8; 32], NativeError> {
        if !self.active || self.failed {
            return Err(NativeError(-1));
        }
        let mut output = [0; 32];
        // SAFETY: exclusive live context and complete SHA-256 output buffer.
        let code = unsafe { mbedtls_sha256_finish(self.context.as_ptr(), output.as_mut_ptr()) };
        self.result(code)?;
        self.active = false;
        Ok(output)
    }
}
impl Drop for NativeSha {
    fn drop(&mut self) {
        // SAFETY: calls are synchronous, no other owners/callbacks retain storage.
        unsafe {
            mbedtls_sha256_free(self.context.as_ptr());
            dealloc(self.context.as_ptr().cast(), self.allocation);
        }
    }
}
