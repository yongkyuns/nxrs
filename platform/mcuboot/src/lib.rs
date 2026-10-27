//! Bare-boot firmware wiring; unbound library builds export no SDK callback.
#![no_std]

#[cfg(nxrs_boot_policy_bound)]
mod bound {
    // Generated only from the independently pinned public application policy.
    include!(env!("NXRS_BOOT_TRUST_RUST"));
    mcuboot_policy::bind_image_check_hook!(TRUST);

    // A panic must never return an accepting image or unwind through C. The
    // independently enabled SDK watchdog remains active; physical reset timing
    // and stack qualification remain separate acceptance gates.
    #[panic_handler]
    fn panic(_: &core::panic::PanicInfo<'_>) -> ! {
        loop {
            core::hint::spin_loop();
        }
    }
}
