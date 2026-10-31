//! Direct synchronous MCUboot SDK bindings. The SDK opens/closes its native
//! flash resources within each call; one Rust owner excludes concurrent calls.
use crate::staging::Slot;
use crate::{DeviceError, State};
use core::ffi::c_int;
use core::ptr::NonNull;

// Private pinned SDK layouts, checked by the target-header ABI witness.
#[repr(C)]
struct FlashArea {
    id: u8,
    device: u8,
    padding: u16,
    offset: u32,
    size: u32,
    path: *const core::ffi::c_char,
}
#[repr(C)]
struct Sector {
    offset: u32,
    size: u32,
}

unsafe extern "C" {
    fn flash_area_id_from_image_slot(slot: c_int) -> c_int;
    fn flash_area_open(id: u8, area: *mut *const FlashArea) -> c_int;
    fn flash_area_close(area: *const FlashArea);
    fn flash_area_align(area: *const FlashArea) -> u32;
    fn flash_area_get_sector(area: *const FlashArea, offset: i64, sector: *mut Sector) -> c_int;
    fn flash_area_erase(area: *const FlashArea, offset: u32, length: u32) -> c_int;
    fn flash_area_write(area: *const FlashArea, offset: u32, data: *const u8, length: u32)
    -> c_int;
    fn flash_area_read(area: *const FlashArea, offset: u32, data: *mut u8, length: u32) -> c_int;
    fn boot_trailer_sz(alignment: u32) -> u32;
    fn boot_swap_type_multi(image: c_int) -> c_int;
    fn boot_set_pending_multi(image: c_int, permanent: c_int) -> c_int;
    fn boot_set_confirmed_multi(image: c_int) -> c_int;
}

pub(super) struct Secondary {
    area: NonNull<FlashArea>,
    size: u32,
    capacity: u32,
    alignment: u32,
}
impl Secondary {
    pub(super) fn open() -> Result<Self, DeviceError> {
        let mut area = core::ptr::null();
        // SAFETY: the enclosing provider holds the exclusive synchronous SDK
        // lease. The SDK supplies a static descriptor, retained until close.
        unsafe {
            let id = u8::try_from(flash_area_id_from_image_slot(1))
                .map_err(|_| DeviceError::Unsupported)?;
            result(flash_area_open(id, &mut area))?;
            let area = NonNull::new(area.cast_mut()).ok_or(DeviceError::InvalidState)?;
            let mut slot = Self {
                size: area.as_ref().size,
                area,
                capacity: 0,
                alignment: 0,
            };
            let mut sector = Sector { offset: 0, size: 0 };
            result(flash_area_get_sector(area.as_ptr(), 0, &mut sector))?;
            let alignment = flash_area_align(area.as_ptr());
            if sector.offset != 0
                || sector.size == 0
                || slot.size % sector.size != 0
                || alignment == 0
                || !alignment.is_power_of_two()
                || alignment > 256
            {
                return Err(DeviceError::InvalidState);
            }
            slot.capacity = slot
                .size
                .checked_sub(boot_trailer_sz(alignment))
                .ok_or(DeviceError::InvalidState)?
                & !(alignment - 1);
            slot.alignment = alignment;
            Ok(slot)
        }
    }
    fn checked(&self, offset: u32, length: usize) -> Result<u32, DeviceError> {
        let length = u32::try_from(length).map_err(|_| DeviceError::Capacity)?;
        if offset > self.capacity || length > self.capacity - offset {
            return Err(DeviceError::Capacity);
        }
        Ok(length)
    }
}
impl Drop for Secondary {
    fn drop(&mut self) {
        // SAFETY: exactly one successful SDK open; synchronous operations have joined.
        unsafe {
            flash_area_close(self.area.as_ptr());
        }
    }
}
impl Slot for Secondary {
    fn capacity(&self) -> u32 {
        self.capacity
    }
    fn alignment(&self) -> u32 {
        self.alignment
    }
    fn erase(&mut self) -> Result<(), DeviceError> {
        // SAFETY: only the inactive SDK area, with validated whole-sector size.
        unsafe { result(flash_area_erase(self.area.as_ptr(), 0, self.size)) }
    }
    fn write(&mut self, offset: u32, data: &[u8]) -> Result<(), DeviceError> {
        let length = self.checked(offset, data.len())?;
        if offset % self.alignment != 0 || length % self.alignment != 0 {
            return Err(DeviceError::InvalidInput);
        }
        // SAFETY: borrowed buffer covers length; bounded aligned inactive-slot range.
        unsafe {
            result(flash_area_write(
                self.area.as_ptr(),
                offset,
                data.as_ptr(),
                length,
            ))
        }
    }
    fn read(&mut self, offset: u32, data: &mut [u8]) -> Result<(), DeviceError> {
        let length = self.checked(offset, data.len())?;
        // SAFETY: exclusive borrowed output covers the checked inactive-slot range.
        unsafe {
            result(flash_area_read(
                self.area.as_ptr(),
                offset,
                data.as_mut_ptr(),
                length,
            ))
        }
    }
}

// Public MCUboot enum values, checked against target headers by the SDK witness.
pub(super) fn state() -> Result<State, DeviceError> {
    // SAFETY: the provider holds the exclusive SDK lease; image zero is selected
    // by the single-image product configuration. There are no retained callbacks.
    match unsafe { boot_swap_type_multi(0) } {
        1 => Ok(State::Stable),
        2 => Ok(State::Pending),
        4 => Ok(State::Trial),
        value if value < 0 => Err(DeviceError::Io),
        _ => Err(DeviceError::InvalidState),
    }
}
pub(super) fn request_trial() -> Result<(), DeviceError> {
    // SAFETY: exclusive synchronous SDK operation; permanent confirmation is off.
    result(unsafe { boot_set_pending_multi(0, 0) })
}
pub(super) fn confirm() -> Result<(), DeviceError> {
    // SAFETY: exclusive synchronous SDK operation; the Rust controller admits
    // this only for an executing trial image, after caller-established health.
    result(unsafe { boot_set_confirmed_multi(0) })
}
fn result(value: c_int) -> Result<(), DeviceError> {
    if value == 0 {
        Ok(())
    } else {
        Err(DeviceError::Io)
    }
}
