//! Private raw-MTD resource with product-configured Rust partition views.
use crate::{
    controller::Stager,
    worker::{CryptoVerifier, Flash, BUFFER},
    Error, Policy,
};
use core::{
    ffi::{c_int, c_void},
    ptr::NonNull,
};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

include!(env!("NXRS_UPDATE_LAYOUT_RS"));
#[repr(C)]
struct Mtd {
    _private: [u8; 0],
}
unsafe extern "C" {
    fn nxrs_update_root() -> *mut Mtd;
    fn nxrs_update_geometry(device: *mut Mtd, values: *mut u32) -> c_int;
    fn nxrs_update_storage_config(values: *mut u32);
    fn nxrs_update_has_write(device: *mut Mtd) -> c_int;
    fn nxrs_update_read(device: *mut Mtd, offset: u32, bytes: *mut u8, length: u32) -> isize;
    fn nxrs_update_write(device: *mut Mtd, offset: u32, bytes: *const u8, length: u32) -> isize;
    fn nxrs_update_erase(device: *mut Mtd, block: u32) -> c_int;
    fn nxrs_update_internal_alloc() -> *mut c_void;
    fn nxrs_update_internal_free(memory: *mut c_void);
}
static LEASE: AtomicBool = AtomicBool::new(false);
struct Native {
    device: NonNull<Mtd>,
    buffer: Option<NonNull<u8>>,
    closed: bool,
}
// SAFETY: one worker owns the internal buffer and the capability's exclusive
// lease. The kernel owns the static SDK MTD object. Each operation is synchronous;
// the PSRAM-stack driver waits uninterruptibly for its LPWORK operation to finish.
unsafe impl Send for Native {}
impl Native {
    fn acquire(policy: &Policy) -> Result<Self, Error> {
        if policy.layout != RESOURCE_LAYOUT {
            return Err(Error::Geometry);
        }
        LEASE
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| Error::Busy)?;
        // SAFETY: selected target header factory returns the kernel's static
        // native MTD object; no Rust struct layout or C-owned wrapper is invented.
        let Some(device) = NonNull::new(unsafe { nxrs_update_root() }) else {
            LEASE.store(false, Ordering::Release);
            return Err(Error::Missing);
        };
        let mut native = Self {
            device,
            buffer: None,
            closed: false,
        };
        native.validate(policy)?;
        // SAFETY: target kernel/internal allocator, excluding PSRAM while SPI
        // caches are disabled. Rust owns this allocation until synchronous I/O ends.
        native.buffer =
            Some(NonNull::new(unsafe { nxrs_update_internal_alloc() }.cast()).ok_or(Error::Io)?);
        Ok(native)
    }
    fn bounds(&self, extent: u32, offset: u32, length: usize) -> Result<(), Error> {
        if self.closed {
            return Err(Error::Closed);
        }
        if length > BUFFER || u64::from(offset) + length as u64 > u64::from(extent) {
            return Err(Error::Geometry);
        }
        Ok(())
    }
    fn read(&mut self, base: u32, extent: u32, offset: u32, bytes: &mut [u8]) -> Result<(), Error> {
        self.bounds(extent, offset, bytes.len())?;
        let absolute = base.checked_add(offset).ok_or(Error::Geometry)?;
        let buffer = self.buffer.ok_or(Error::Closed)?;
        // SAFETY: exclusive bounded internal buffer and actual native SDK object.
        if unsafe {
            nxrs_update_read(
                self.device.as_ptr(),
                absolute,
                buffer.as_ptr(),
                bytes.len() as u32,
            )
        } != bytes.len() as isize
        {
            return Err(Error::Io);
        }
        // SAFETY: native read completed; no callback retains the initialized bytes.
        bytes.copy_from_slice(unsafe { core::slice::from_raw_parts(buffer.as_ptr(), bytes.len()) });
        Ok(())
    }
}
impl Flash for Native {
    fn validate(&mut self, policy: &Policy) -> Result<(), Error> {
        if policy.layout != RESOURCE_LAYOUT
            || RESOURCE_LAYOUT.erase_bytes != 4096
            || RESOURCE_LAYOUT.write_align != 4
            || RESOURCE_LAYOUT.max_align != 8
            || self.closed
        {
            return Err(Error::Geometry);
        }
        let mut storage = [0; 2];
        // SAFETY: actual target-header storage configuration, exactly two scalars.
        unsafe { nxrs_update_storage_config(storage.as_mut_ptr()) };
        let start = u64::from(storage[0]);
        let end = start + u64::from(storage[1]);
        let firmware = [
            (RESOURCE_LAYOUT.primary_offset, RESOURCE_LAYOUT.slot_bytes),
            (RESOURCE_LAYOUT.secondary_offset, RESOURCE_LAYOUT.slot_bytes),
            (
                RESOURCE_LAYOUT.scratch_offset,
                RESOURCE_LAYOUT.scratch_bytes,
            ),
        ];
        let firmware_end = firmware
            .iter()
            .map(|&(offset, size)| u64::from(offset) + u64::from(size))
            .max()
            .unwrap();
        if start < firmware_end
            || storage[1] == 0
            || storage[0] % RESOURCE_LAYOUT.erase_bytes != 0
            || storage[1] % RESOURCE_LAYOUT.erase_bytes != 0
            || end > u64::from(RESOURCE_LAYOUT.flash_bytes)
        {
            return Err(Error::Geometry);
        }
        let mut values = [0; 3];
        // SAFETY: header-defined native geometry is copied to exactly three scalars.
        if unsafe { nxrs_update_geometry(self.device.as_ptr(), values.as_mut_ptr()) } < 0
            || values[0] == 0
            || values[1] != RESOURCE_LAYOUT.erase_bytes
            || u64::from(values[1]) * u64::from(values[2]) != u64::from(RESOURCE_LAYOUT.flash_bytes)
            || unsafe { nxrs_update_has_write(self.device.as_ptr()) } < 0
        {
            return Err(Error::Geometry);
        }
        Ok(())
    }
    fn read_primary(&mut self, offset: u32, bytes: &mut [u8]) -> Result<(), Error> {
        self.read(
            RESOURCE_LAYOUT.primary_offset,
            RESOURCE_LAYOUT.slot_bytes,
            offset,
            bytes,
        )
    }
    fn read_secondary(&mut self, offset: u32, bytes: &mut [u8]) -> Result<(), Error> {
        self.read(
            RESOURCE_LAYOUT.secondary_offset,
            RESOURCE_LAYOUT.slot_bytes,
            offset,
            bytes,
        )
    }
    fn read_scratch(&mut self, offset: u32, bytes: &mut [u8]) -> Result<(), Error> {
        self.read(
            RESOURCE_LAYOUT.scratch_offset,
            RESOURCE_LAYOUT.scratch_bytes,
            offset,
            bytes,
        )
    }
    fn erase_secondary(&mut self, block: u32) -> Result<(), Error> {
        self.bounds(RESOURCE_LAYOUT.slot_bytes, 0, 0)?;
        if block >= RESOURCE_LAYOUT.slot_bytes / RESOURCE_LAYOUT.erase_bytes {
            return Err(Error::Geometry);
        }
        let absolute = RESOURCE_LAYOUT.secondary_offset / RESOURCE_LAYOUT.erase_bytes + block;
        // SAFETY: Rust restricts the native block operation to the configured
        // secondary view. Primary and scratch are never mutation targets.
        if unsafe { nxrs_update_erase(self.device.as_ptr(), absolute) } != 1 {
            return Err(Error::Io);
        }
        Ok(())
    }
    fn write_secondary(&mut self, offset: u32, bytes: &[u8]) -> Result<(), Error> {
        self.bounds(RESOURCE_LAYOUT.slot_bytes, offset, bytes.len())?;
        if bytes.is_empty() || offset % 4 != 0 || bytes.len() % 4 != 0 {
            return Err(Error::Geometry);
        }
        let absolute = RESOURCE_LAYOUT
            .secondary_offset
            .checked_add(offset)
            .ok_or(Error::Geometry)?;
        let buffer = self.buffer.ok_or(Error::Closed)?;
        // SAFETY: copy with caches enabled, then keep internal memory alive and
        // exclusive until the native call returns. No arbitrary SDK buffer escape.
        unsafe { core::slice::from_raw_parts_mut(buffer.as_ptr(), bytes.len()) }
            .copy_from_slice(bytes);
        if unsafe {
            nxrs_update_write(
                self.device.as_ptr(),
                absolute,
                buffer.as_ptr(),
                bytes.len() as u32,
            )
        } != bytes.len() as isize
        {
            return Err(Error::Io);
        }
        Ok(())
    }
    fn close(&mut self) -> Result<(), Error> {
        if self.closed {
            return Ok(());
        }
        // SAFETY: only called after synchronous I/O quiescence; the kernel keeps
        // its MTD object. Free our sole native buffer before publishing the lease.
        if let Some(buffer) = self.buffer.take() {
            unsafe {
                nxrs_update_internal_free(buffer.as_ptr().cast());
            }
        }
        self.closed = true;
        LEASE.store(false, Ordering::Release);
        Ok(())
    }
}
impl Drop for Native {
    fn drop(&mut self) {
        let _ = self.close();
    }
}
pub fn open<C: crate::Crypto + Send + 'static>(
    policy: Policy,
    crypto: C,
    retirement_timeout: Duration,
    wake: impl Fn() + Send + Sync + 'static,
) -> Result<Stager, Error> {
    Stager::spawn(
        Native::acquire(&policy)?,
        CryptoVerifier(crypto),
        policy,
        32768,
        retirement_timeout,
        wake,
    )
}
