//! MCUboot scratch-swap trailer protocol; Rust owns the admission decision.
#![forbid(unsafe_code)]
use crate::Error;

pub(crate) const MAGIC: [u8; 16] = [
    0x77, 0xc2, 0x95, 0xf3, 0x60, 0xd2, 0xef, 0x7f, 0x35, 0x52, 0x50, 0x0f, 0x2c, 0xb6, 0x79, 0x80,
];
pub(crate) const TAIL: usize = 48;

pub(crate) fn idle_primary(tail: &[u8; TAIL]) -> bool {
    if tail.iter().all(|byte| *byte == 255) {
        return true;
    }
    // A confirmed image is stable. Test images with copy-done but no image-ok
    // retain their secondary recovery image and must not admit another stage.
    tail[32..] == MAGIC
        && tail[24] == 1
        && tail[25..32].iter().all(|byte| *byte == 255)
        && matches!(tail[16], 1 | 255)
        && tail[17..24].iter().all(|byte| *byte == 255)
}

pub(crate) fn idle_secondary(tail: &[u8; TAIL]) -> Result<(), Error> {
    // Require an entirely erased control tail: pending, partial or unknown
    // control state is left for boot/recovery ownership, never erased here.
    if tail.iter().all(|byte| *byte == 255) {
        Ok(())
    } else {
        Err(Error::BootBusy)
    }
}
