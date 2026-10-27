//! Read-only policy checks at the actual MCUboot image-check hook.
//!
//! This boot-SDK integration is separate from the application update HAL. It
//! cannot authenticate or activate an image. A pass must continue to the SDK's
//! ordinary signature/floor validation. See `bind_image_check_hook!`.
#![no_std]
#![deny(unsafe_op_in_unsafe_fn)]

use core::ffi::{c_int, c_uint, c_void};
use core::ptr::NonNull;
pub use firmware_image;
use firmware_image::{check_policy, IncrementalHash, PolicyError, ReadAt, Trust};

unsafe extern "C" {
    fn nxrs_boot_watchdog_feed();
    fn nxrs_boot_sha_size() -> usize;
    fn nxrs_boot_sha_align() -> usize;
    fn nxrs_boot_key_count() -> c_int;
    fn nxrs_boot_key_data() -> *const u8;
    fn nxrs_boot_key_length() -> c_uint;
    fn nxrs_boot_area_size(area: *const c_void) -> u32;
    fn nxrs_boot_area_offset(area: *const c_void) -> u32;
    fn flash_area_id_from_multi_image_slot(image: c_int, slot: c_int) -> c_int;
    fn flash_area_open(id: u8, area: *mut *const c_void) -> c_int;
    fn flash_area_close(area: *const c_void);
    fn flash_area_read(area: *const c_void, offset: u32, output: *mut c_void, bytes: u32) -> c_int;
    fn mbedtls_sha256_init(context: *mut c_void);
    fn mbedtls_sha256_free(context: *mut c_void);
    fn mbedtls_sha256_starts(context: *mut c_void, is224: c_int) -> c_int;
    fn mbedtls_sha256_update(context: *mut c_void, input: *const u8, bytes: usize) -> c_int;
    fn mbedtls_sha256_finish(context: *mut c_void, output: *mut u8) -> c_int;
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Slot,
    Root,
    Read,
    Hash,
    Policy,
}

// The backing storage is borrowed before initialization, so moving the SHA
// owner never moves the initialized native object. No heap, global context,
// callbacks, native borrowed input, or reentrant/asynchronous SDK calls.
#[repr(C, align(8))]
struct Storage([u8; 256]);
struct Sha<'a> {
    storage: &'a mut Storage,
    active: bool,
    failed: bool,
}
impl<'a> Sha<'a> {
    fn new(storage: &'a mut Storage) -> Result<Self, Error> {
        // SAFETY: getters compile with exactly the SDK headers/config in use.
        let (size, align) = unsafe { (nxrs_boot_sha_size(), nxrs_boot_sha_align()) };
        if size == 0
            || size > storage.0.len()
            || align == 0
            || align > 8
            || !align.is_power_of_two()
        {
            return Err(Error::Hash);
        }
        // SAFETY: exclusive stable SDK-sized/aligned storage; synchronous init.
        unsafe { mbedtls_sha256_init(storage.0.as_mut_ptr().cast()) };
        Ok(Self {
            storage,
            active: false,
            failed: false,
        })
    }
    fn pointer(&mut self) -> *mut c_void {
        self.storage.0.as_mut_ptr().cast()
    }
    fn result(&mut self, status: c_int) -> Result<(), Error> {
        if status != 0 {
            self.failed = true;
            self.active = false;
            Err(Error::Hash)
        } else {
            Ok(())
        }
    }
}
impl IncrementalHash for Sha<'_> {
    type Error = Error;
    fn reset(&mut self) -> Result<(), Error> {
        if self.failed {
            return Err(Error::Hash);
        }
        // SAFETY: exclusive initialized SDK context, no retained borrowed inputs.
        let result = unsafe { mbedtls_sha256_starts(self.pointer(), 0) };
        self.result(result)?;
        self.active = true;
        Ok(())
    }
    fn update(&mut self, bytes: &[u8]) -> Result<(), Error> {
        if self.failed || !self.active {
            return Err(Error::Hash);
        }
        // SAFETY: source remains live through this synchronous SDK call.
        let result = unsafe { mbedtls_sha256_update(self.pointer(), bytes.as_ptr(), bytes.len()) };
        self.result(result)?;
        // SAFETY: SDK watchdog macro, after successful bounded hashing progress.
        unsafe { nxrs_boot_watchdog_feed() };
        Ok(())
    }
    fn finish(&mut self) -> Result<[u8; 32], Error> {
        if self.failed || !self.active {
            return Err(Error::Hash);
        }
        let mut output = [0; 32];
        // SAFETY: exclusive initialized SDK context and full output buffer.
        let result = unsafe { mbedtls_sha256_finish(self.pointer(), output.as_mut_ptr()) };
        self.result(result)?;
        self.active = false;
        Ok(output)
    }
}
impl Drop for Sha<'_> {
    fn drop(&mut self) {
        // SAFETY: synchronous software context; no outstanding calls/borrowers.
        unsafe { mbedtls_sha256_free(self.pointer()) };
    }
}

struct Area {
    pointer: NonNull<c_void>,
    bytes: usize,
}
impl Drop for Area {
    fn drop(&mut self) {
        // SAFETY: only synchronous reads; closing this handle cannot free the
        // SDK's independently held validation handle or transfer flash ownership.
        unsafe { flash_area_close(self.pointer.as_ptr()) };
    }
}
impl ReadAt for Area {
    type Error = Error;
    fn length(&self) -> usize {
        self.bytes
    }
    fn read_at(&mut self, offset: usize, output: &mut [u8]) -> Result<(), Error> {
        if output.len() > 128 || offset > self.bytes || output.len() > self.bytes - offset {
            return Err(Error::Read);
        }
        // SAFETY: validated slot-relative range within a live SDK area; native
        // flash read returns synchronously and retains no output pointer.
        let result = unsafe {
            flash_area_read(
                self.pointer.as_ptr(),
                offset as u32,
                output.as_mut_ptr().cast(),
                output.len() as u32,
            )
        };
        if result == 0 {
            // SAFETY: SDK watchdog macro, only after bounded read progress.
            unsafe { nxrs_boot_watchdog_feed() };
            Ok(())
        } else {
            Err(Error::Read)
        }
    }
}

/// Check the compiled root, actual SDK slot geometry and signed manifest policy.
/// A pass is only permission to run the stock SDK signature/floor check.
///
/// # Safety
/// The caller must be the single, nonreentrant prekernel boot execution context,
/// with exclusive control of flash until stock validation and boot selection
/// finish. The linked SDK must use the qualified FIH-off, synchronous flash and
/// software SHA ABI; compile `src/ffi/sdk.c` with those exact SDK headers/flags.
/// SDK operations must return normally, without unwinding/longjmp or retaining
/// buffers. No concurrent writer, DMA, untrusted callback or mutable key table is
/// permitted. This function never acquires exclusivity merely by opening an area.
pub unsafe fn check_slot(trust: &Trust<'_>, image: c_int, slot: c_int) -> Result<(), Error> {
    trust.validate().map_err(|_| Error::Policy)?;
    if image != 0 || !matches!(slot, 0 | 1) {
        return Err(Error::Slot);
    }
    let mut storage = Storage([0; 256]);
    let mut hash = Sha::new(&mut storage)?;
    // SAFETY: linked SDK's immutable sole root under the caller's boot contract.
    if unsafe { nxrs_boot_key_count() } != 1 {
        return Err(Error::Root);
    }
    let (data, length) = unsafe { (nxrs_boot_key_data(), nxrs_boot_key_length()) };
    if data.is_null() || length != 398 {
        return Err(Error::Root);
    }
    // SAFETY: actual compiled canonical public DER table of the measured length.
    let key = unsafe { core::slice::from_raw_parts(data, length as usize) };
    if key[..9] != [0x30, 0x82, 0x01, 0x8a, 0x02, 0x82, 0x01, 0x81, 0]
        || key[393..] != [2, 3, 1, 0, 1]
        || key[9] & 0x80 == 0
    {
        return Err(Error::Root);
    }
    hash.reset()?;
    hash.update(key)?;
    if hash.finish()? != trust.key_sha256 {
        return Err(Error::Root);
    }
    // SAFETY: prevalidated supported single-image slot; SDK map is immutable.
    let id = unsafe { flash_area_id_from_multi_image_slot(image, slot) };
    let id = u8::try_from(id).map_err(|_| Error::Slot)?;
    let mut pointer = core::ptr::null();
    // SAFETY: correctly typed local output for synchronous SDK area acquisition.
    if unsafe { flash_area_open(id, &mut pointer) } != 0 {
        return Err(Error::Slot);
    }
    let pointer = NonNull::new(pointer.cast_mut()).ok_or(Error::Slot)?;
    // Own the successful nonnull acquisition immediately, including mismatches.
    let mut area = Area { pointer, bytes: 0 };
    // SAFETY: live SDK area; getters read the actual target struct fields.
    let (bytes, offset) = unsafe {
        (
            nxrs_boot_area_size(area.pointer.as_ptr()),
            nxrs_boot_area_offset(area.pointer.as_ptr()),
        )
    };
    let expected = if slot == 0 {
        trust.layout.primary_offset
    } else {
        trust.layout.secondary_offset
    };
    if bytes != trust.layout.slot_bytes || offset != expected {
        return Err(Error::Slot);
    }
    area.bytes = bytes as usize;
    check_policy(&mut area, trust, &mut hash).map_err(|error| match error {
        PolicyError::Format(_) => Error::Policy,
        PolicyError::Read(_) => Error::Read,
        PolicyError::Hash(_) => Error::Hash,
    })?;
    Ok(())
}

/// Export the actual native SDK callback with an independently trusted policy.
///
/// The boot firmware invoking this macro must uphold `check_slot`'s safety
/// contract for every SDK callback, compile the exact header shim and keep
/// `MCUBOOT_IMAGE_ACCESS_HOOKS` enabled. The macro supports only the FIH-off ABI.
/// It NEVER returns FIH_SUCCESS: a pass continues stock SDK authentication.
#[macro_export]
macro_rules! bind_image_check_hook {
    ($trust:expr) => {
        #[no_mangle]
        pub unsafe extern "C" fn boot_image_check_hook(
            image: core::ffi::c_int,
            slot: core::ffi::c_int,
        ) -> core::ffi::c_int {
            unsafe extern "C" {
                static FIH_BOOT_HOOK_REGULAR: core::ffi::c_int;
                static FIH_FAILURE: core::ffi::c_int;
            }
            // SAFETY: the invoking boot firmware owns the SDK callback contract.
            match unsafe { $crate::check_slot(&$trust, image, slot) } {
                Ok(()) => unsafe { FIH_BOOT_HOOK_REGULAR },
                Err(_) => unsafe { FIH_FAILURE },
            }
        }
    };
}
