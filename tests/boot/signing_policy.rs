// Real MCUboot SDK witness; fixture key data never enters a product build.
use super::*;

fn builder() -> ImagesBuilder {
    assert!(Caps::RSA3072.present());
    assert!(Caps::ValidatePrimarySlot.present());
    assert!(Caps::HwRollbackProtection.present());
    assert!(Caps::SwapUsingScratch.present());
    assert_eq!(Caps::get_num_images(), 1);
    assert_eq!(c::boot_max_align(), 8);
    ImagesBuilder::new(DeviceName::K64f, 4, 0xff).unwrap()
}

fn installed(data: &[u8]) -> Images {
    let mut images = builder().make_no_upgrade_image(&NO_DEPS, ImageManipulation::None);
    let slot = &images.images[0].slots[0];
    let dev = images.flash.get_mut(&slot.dev_id).unwrap();
    dev.erase(slot.base_off, slot.len).unwrap();
    let mut bytes = data.to_vec();
    while bytes.len() % 4 != 0 {
        bytes.push(0xff);
    }
    dev.write(slot.base_off, &bytes).unwrap();
    images
}

fn boot(images: &mut Images) -> c::BootGoResult {
    c::boot_go(&mut images.flash, &images.areadesc, None, None, true)
}

#[test]
fn fresh_authority_image_is_accepted() {
    c::reset_security_counters();
    let mut valid = installed(include_bytes!("application.bin"));
    let result = boot(&mut valid);
    assert!(result.success_no_asserts());
    assert_eq!(result.resp().unwrap().image_off, 0x20000);
    assert_eq!(c::get_security_counter(0), 7);
}

#[test]
fn other_authority_key_is_refused() {
    c::reset_security_counters();
    let mut wrong = installed(include_bytes!("wrong-key.bin"));
    assert!(!boot(&mut wrong).success());
    assert_eq!(c::get_security_counter(0), 0);
}

#[test]
fn sdk_example_key_is_refused() {
    c::reset_security_counters();
    let mut sample = builder().make_no_upgrade_image(&NO_DEPS, ImageManipulation::None);
    assert!(!boot(&mut sample).success());
    assert_eq!(c::get_security_counter(0), 0);
}

#[test]
fn stored_security_floor_is_enforced() {
    c::reset_security_counters();
    c::set_security_counter(0, 8);
    let mut old = installed(include_bytes!("application.bin"));
    assert!(!boot(&mut old).success());
    assert_eq!(c::get_security_counter(0), 8);
}

#[test]
fn authenticated_image_mutation_is_refused() {
    c::reset_security_counters();
    let mut changed = include_bytes!("application.bin").to_vec();
    changed[64] ^= 1;
    let mut corrupt = installed(&changed);
    assert!(!boot(&mut corrupt).success());
    assert_eq!(c::get_security_counter(0), 0);
}
