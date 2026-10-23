// Actual SDK loader -> production native callback -> Rust policy -> SDK RSA.
use super::*;
use mcuboot_policy::firmware_image::{self, FormatError, Trust};
use std::sync::atomic::{AtomicUsize, Ordering};

#[derive(Clone, Copy)]
enum Expected {
    Accept,
    Crypto,
    Format(FormatError),
}
struct Case {
    label: &'static str,
    image: &'static [u8],
    floor: u32,
    expected: Expected,
    native_signed_end: Option<usize>,
}
include!(env!("NXRS_BOOT_HOOK_CASES"));
static CALLS: AtomicUsize = AtomicUsize::new(0);
static CONTROL: AtomicUsize = AtomicUsize::new(0);
fn callback_trust() -> Trust<'static> {
    CALLS.fetch_add(1, Ordering::SeqCst);
    let mut trust = trusted();
    if CONTROL.load(Ordering::SeqCst) == 1 {
        trust.key_sha256[0] ^= 1;
    }
    trust
}
// SAFETY: simulator's boot_go serializes the SDK, reads are synchronous and do
// not longjmp; cuts only occur in writes, outside this read-only callback.
mcuboot_policy::bind_image_check_hook!(callback_trust());

fn installed(data: &[u8], slot: usize) -> Images {
    let mut images = ImagesBuilder::new(DeviceName::K64f, 4, 0xff)
        .unwrap()
        .make_no_upgrade_image(&NO_DEPS, ImageManipulation::None);
    // Erase both upstream sample images; an invalid primary must not fall back
    // to a valid sample root or an unrelated candidate.
    for info in &images.images[0].slots {
        images
            .flash
            .get_mut(&info.dev_id)
            .unwrap()
            .erase(info.base_off, info.len)
            .unwrap();
    }
    let info = &images.images[0].slots[slot];
    let mut bytes = data.to_vec();
    while bytes.len() % 4 != 0 {
        bytes.push(0xff);
    }
    images
        .flash
        .get_mut(&info.dev_id)
        .unwrap()
        .write(info.base_off, &bytes)
        .unwrap();
    images
}
fn boot(images: &mut Images) -> c::BootGoResult {
    c::boot_go(&mut images.flash, &images.areadesc, None, None, true)
}
fn reset(floor: u32) {
    c::reset_security_counters();
    c::set_security_counter(0, floor);
    CALLS.store(0, Ordering::SeqCst);
    CONTROL.store(0, Ordering::SeqCst);
}

#[test]
fn signed_wrong_manifest_is_accepted_only_without_hook() {
    for case in CASES
        .iter()
        .filter(|c| c.label.starts_with("signed-wrong-") && c.label != "signed-wrong-pss-salt")
    {
        assert!(case.native_signed_end.is_some());
        reset(case.floor);
        let mut images = installed(case.image, 0);
        let result = boot(&mut images);
        assert_eq!(result.asserts(), 0, "{}", case.label);
        assert!(!result.interrupted(), "{}", case.label);
        assert_eq!(result.success_no_asserts(), !HOOK_ENABLED, "{}", case.label);
        assert_eq!(
            CALLS.load(Ordering::SeqCst) > 0,
            HOOK_ENABLED,
            "{}",
            case.label
        );
        assert_eq!(
            c::get_security_counter(0),
            if HOOK_ENABLED { case.floor } else { 7 }
        );
    }
    // Move the actual SDK area while retaining the original independently
    // approved policy and exactly the same valid signed image. This avoids a
    // malformed-policy/layout-hash failure masquerading as a geometry check.
    reset(1);
    let mut images = installed(CASES[0].image, 0);
    let dev_id = images.images[0].slots[0].dev_id;
    let dev = images.flash.get_mut(&dev_id).unwrap();
    dev.erase(0x1f000, 0x21000).unwrap();
    let mut bytes = CASES[0].image.to_vec();
    while bytes.len() % 4 != 0 {
        bytes.push(0xff);
    }
    dev.write(0x1f000, &bytes).unwrap();
    let mut desc = AreaDesc::new();
    desc.add_flash_sectors(dev_id, dev);
    desc.add_image(0x1f000, 0x20000, FlashId::Image0, dev_id);
    desc.add_image(0x40000, 0x20000, FlashId::Image1, dev_id);
    desc.add_image(0x60000, 0x1000, FlashId::ImageScratch, dev_id);
    images.areadesc = Rc::new(desc);
    let result = boot(&mut images);
    assert_eq!(result.asserts(), 0);
    assert!(!result.interrupted());
    assert_eq!(result.success(), !HOOK_ENABLED);
    assert_eq!(CALLS.load(Ordering::SeqCst) > 0, HOOK_ENABLED);
    assert_eq!(c::get_security_counter(0), if HOOK_ENABLED { 1 } else { 7 });
}

#[test]
fn all_signed_cases_keep_stock_signature_and_floor_enforcement() {
    if !HOOK_ENABLED {
        return;
    } // Above supplies the required disabled-hook control.
    for case in CASES {
        reset(case.floor);
        let mut images = installed(case.image, 0);
        let expected =
            matches!(case.expected, Expected::Accept) || case.label == "trailing-preconfirmation";
        if let Expected::Format(error) = case.expected {
            let _ = error;
        }
        let result = boot(&mut images);
        assert_eq!(result.asserts(), 0, "{}", case.label);
        assert!(!result.interrupted(), "{}", case.label);
        assert_eq!(result.success_no_asserts(), expected, "{}", case.label);
        assert_eq!(
            c::get_security_counter(0),
            if expected {
                u32::from_le_bytes(
                    case.image[32
                        + u32::from_le_bytes(case.image[12..16].try_into().unwrap()) as usize
                        + 8
                        ..32 + u32::from_le_bytes(case.image[12..16].try_into().unwrap()) as usize
                            + 12]
                        .try_into()
                        .unwrap(),
                )
            } else {
                case.floor
            },
            "{}",
            case.label
        );
        println!("BOOT_POLICY_CASE {} passed", case.label);
    }
}

#[test]
fn compiled_root_is_required_and_restored() {
    if !HOOK_ENABLED {
        return;
    }
    reset(1);
    CONTROL.store(1, Ordering::SeqCst);
    let mut images = installed(CASES[0].image, 0);
    let result = boot(&mut images);
    assert_eq!(result.asserts(), 0);
    assert!(!result.interrupted());
    assert!(!result.success());
    assert!(CALLS.load(Ordering::SeqCst) > 0);
    assert_eq!(c::get_security_counter(0), 1);
    reset(1);
    let mut restored = installed(CASES[0].image, 0);
    assert!(boot(&mut restored).success_no_asserts());
    assert_eq!(c::get_security_counter(0), 7);
}

#[test]
fn signed_secondary_is_checked_before_trial_and_revert() {
    if !HOOK_ENABLED {
        return;
    }
    reset(1);
    let mut images = installed(CASES[0].image, 0);
    let slot = &images.images[0].slots[1];
    let mut bytes = CASES[0].image.to_vec();
    while bytes.len() % 4 != 0 {
        bytes.push(0xff);
    }
    images
        .flash
        .get_mut(&slot.dev_id)
        .unwrap()
        .write(slot.base_off, &bytes)
        .unwrap();
    mark_upgrade(&mut images.flash, slot);
    assert!(boot(&mut images).success_no_asserts());
    assert!(CALLS.load(Ordering::SeqCst) >= 2);
    assert_eq!(c::get_security_counter(0), 1);
    assert!(boot(&mut images).success_no_asserts());
    assert_eq!(c::get_security_counter(0), 7);
}

#[test]
fn wrong_policy_secondary_retains_valid_primary() {
    if !HOOK_ENABLED {
        return;
    }
    reset(1);
    let mut images = installed(CASES[0].image, 0);
    let wrong = CASES
        .iter()
        .find(|case| case.label == "signed-wrong-target")
        .unwrap();
    let slot = &images.images[0].slots[1];
    let mut bytes = wrong.image.to_vec();
    while bytes.len() % 4 != 0 {
        bytes.push(0xff);
    }
    images
        .flash
        .get_mut(&slot.dev_id)
        .unwrap()
        .write(slot.base_off, &bytes)
        .unwrap();
    mark_upgrade(&mut images.flash, slot);
    assert!(boot(&mut images).success_no_asserts());
    assert!(CALLS.load(Ordering::SeqCst) >= 2);
    assert_eq!(c::get_security_counter(0), 7);
}
