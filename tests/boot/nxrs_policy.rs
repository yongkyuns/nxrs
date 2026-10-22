// Test-only witnesses of the real MCUboot bootutil, not target hardware evidence.
use super::*;

fn builder() -> ImagesBuilder {
    ImagesBuilder::new(DeviceName::Stm32f4, 4, 0xff).unwrap()
}

fn boot(images: &mut Images) -> c::BootGoResult {
    c::boot_go(&mut images.flash, &images.areadesc, None, None, true)
}

#[test]
fn required_native_policy_is_enabled() {
    assert!(Caps::RSA3072.present());
    assert!(!Caps::RSA2048.present());
    assert!(Caps::ValidatePrimarySlot.present());
    assert!(Caps::HwRollbackProtection.present());
    assert!(Caps::SwapUsingScratch.present());
    assert_eq!(Caps::get_num_images(), 1);
}

#[test]
fn actual_primary_signature_positive_and_negative() {
    c::reset_security_counters();
    let mut valid = builder().make_no_upgrade_image(&NO_DEPS, ImageManipulation::None);
    assert!(boot(&mut valid).success_no_asserts());
    c::reset_security_counters();
    let mut invalid = builder().make_no_upgrade_image(&NO_DEPS, ImageManipulation::BadSignature);
    assert!(!boot(&mut invalid).success());
    assert_eq!(c::get_security_counter(0), 0);
}

#[test]
fn signed_header_body_and_protected_counter_tampering_are_rejected() {
    for field in ["header", "body", "counter"] {
        c::reset_security_counters();
        let mut images = builder().make_image_with_security_counter(Some(1));
        let slot = &images.images[0].slots[0];
        let bytes = &images.images[0].primaries.plain;
        let body_len = u32::from_le_bytes(bytes[12..16].try_into().unwrap()) as usize;
        let offset = match field {
            "header" => 20, // Signed application version.
            "body" => 64,
            "counter" => 32 + body_len + 8, // Protected security-counter TLV value.
            _ => unreachable!(),
        };
        let mut changed = bytes[offset..offset + 4].to_vec();
        changed[0] ^= 1;
        let dev = images.flash.get_mut(&slot.dev_id).unwrap();
        // Inject storage corruption into an already programmed fixture.
        dev.set_verify_writes(false);
        dev.write(slot.base_off + offset, &changed).unwrap();
        dev.set_verify_writes(true);
        assert!(!boot(&mut images).success(), "accepted tampered {field}");
        assert_eq!(c::get_security_counter(0), 0);
    }
}

#[test]
fn invalid_candidate_keeps_authenticated_primary() {
    c::reset_security_counters();
    let mut images = builder().make_bad_secondary_slot_image(ImageManipulation::BadSignature);
    mark_upgrade(&mut images.flash, &images.images[0].slots[1]);
    assert!(boot(&mut images).success_no_asserts());
    assert!(images.verify_images(&images.flash, 0, 0));
}

#[test]
fn trial_reverts_without_advancing_security_counter() {
    c::reset_security_counters();
    c::set_security_counter(0, 1);
    let mut images = builder().make_image_with_security_counter(Some(1));
    mark_upgrade(&mut images.flash, &images.images[0].slots[1]);
    assert!(boot(&mut images).success_no_asserts());
    assert!(images.verify_images(&images.flash, 0, 1));
    assert_eq!(c::get_security_counter(0), 1);
    assert!(boot(&mut images).success_no_asserts());
    assert!(images.verify_images(&images.flash, 0, 0));
    assert_eq!(c::get_security_counter(0), 1);
}

#[test]
fn confirmation_advances_counter_and_refuses_old_signed_image() {
    c::reset_security_counters();
    c::set_security_counter(0, 1);
    let mut images = builder().make_image_with_security_counter(Some(1));
    mark_upgrade(&mut images.flash, &images.images[0].slots[1]);
    assert!(boot(&mut images).success_no_asserts());
    assert_eq!(c::get_security_counter(0), 1);
    mark_permanent_upgrade(&mut images.flash, &images.images[0].slots[0]);
    assert!(boot(&mut images).success_no_asserts());
    assert!(images.verify_images(&images.flash, 0, 1));
    assert_eq!(c::get_security_counter(0), 2);
    let mut old = builder().make_no_upgrade_image(&NO_DEPS, ImageManipulation::None);
    assert!(!boot(&mut old).success());
    assert_eq!(c::get_security_counter(0), 2);
}
