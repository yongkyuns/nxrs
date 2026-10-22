// Appended only to the real SDK's test module by --update-protocol qualification.
// The native bootutil produces every observed trailer; this fixture does not
// reconstruct trailers or substitute a boot state machine.
#[test]
fn actual_boot_trailers_and_interrupted_trial_revert_swaps() {
    fn record(out: &mut Vec<u8>, name: &str, flash: &SimMultiFlash, areas: &AreaDesc) {
        assert!(name.len() <= 63);
        out.push(name.len() as u8);
        out.extend_from_slice(name.as_bytes());
        let (_, slot_size, _) = areas.find(FlashId::Image0).unwrap();
        out.extend_from_slice(&(slot_size as u32).to_le_bytes());
        for id in [FlashId::Image0, FlashId::Image1, FlashId::ImageScratch] {
            let (base, size, device) = areas.find(id).unwrap();
            let mut tail = [0; 48];
            flash[&device]
                .read(base + size - tail.len(), &mut tail)
                .unwrap();
            out.extend_from_slice(&tail);
        }
    }
    assert_eq!(c::boot_max_align(), 8);
    assert_eq!(c::boot_magic_sz(), 16);
    c::reset_security_counters();
    c::set_security_counter(0, 1);
    let mut base = builder().make_image_with_security_counter(Some(1));
    let mut states = Vec::new();
    record(&mut states, "initial", &base.flash, &base.areadesc);
    mark_upgrade(&mut base.flash, &base.images[0].slots[1]);
    record(&mut states, "pending", &base.flash, &base.areadesc);
    let mut completed = base.flash.clone();
    let mut count = 0;
    assert!(
        c::boot_go(&mut completed, &base.areadesc, Some(&mut count), None, true)
            .success_no_asserts()
    );
    let total = -count;
    assert!((2..=4096).contains(&total));
    assert_eq!(c::get_security_counter(0), 1);
    assert!(base.verify_images(&completed, 0, 1));
    record(&mut states, "trial", &completed, &base.areadesc);
    let mut reverted = completed.clone();
    let mut revert_count = 0;
    assert!(c::boot_go(
        &mut reverted,
        &base.areadesc,
        Some(&mut revert_count),
        None,
        true
    )
    .success_no_asserts());
    let revert_total = -revert_count;
    assert!((2..=4096).contains(&revert_total));
    assert!(base.verify_images(&reverted, 0, 0));
    record(&mut states, "reverted", &reverted, &base.areadesc);
    mark_permanent_upgrade(&mut completed, &base.images[0].slots[0]);
    assert!(c::boot_go(&mut completed, &base.areadesc, None, None, true).success_no_asserts());
    assert_eq!(c::get_security_counter(0), 2);
    assert!(base.verify_images(&completed, 0, 1));
    record(&mut states, "confirmed", &completed, &base.areadesc);
    c::reset_security_counters();
    c::set_security_counter(0, 1);
    let mut permanent = base.flash.clone();
    mark_permanent_upgrade(&mut permanent, &base.images[0].slots[1]);
    assert!(c::boot_go(&mut permanent, &base.areadesc, None, None, true).success_no_asserts());
    assert!(base.verify_images(&permanent, 0, 1));
    assert_eq!(c::get_security_counter(0), 2);
    record(&mut states, "permanent", &permanent, &base.areadesc);
    for operation in 1..=total {
        c::reset_security_counters();
        c::set_security_counter(0, 1);
        let mut interrupted = base.flash.clone();
        let mut stop = operation;
        let result = c::boot_go(
            &mut interrupted,
            &base.areadesc,
            Some(&mut stop),
            None,
            false,
        );
        assert!(
            result.interrupted(),
            "missing actual SDK interruption {operation}"
        );
        assert_eq!(stop, 0, "SDK must stop at the requested operation");
        record(
            &mut states,
            &format!("cut-{operation}"),
            &interrupted,
            &base.areadesc,
        );
        assert!(
            c::boot_go(&mut interrupted, &base.areadesc, None, None, true).success_no_asserts()
        );
        assert!(base.verify_images(&interrupted, 0, 1));
        assert_eq!(c::get_security_counter(0), 1);
        record(
            &mut states,
            &format!("trial-resumed-{operation}"),
            &interrupted,
            &base.areadesc,
        );
        assert!(
            c::boot_go(&mut interrupted, &base.areadesc, None, None, true).success_no_asserts()
        );
        assert!(base.verify_images(&interrupted, 0, 0));
        assert_eq!(c::get_security_counter(0), 1);
        record(
            &mut states,
            &format!("reverted-{operation}"),
            &interrupted,
            &base.areadesc,
        );
    }
    for operation in 1..=revert_total {
        c::reset_security_counters();
        c::set_security_counter(0, 1);
        let mut interrupted = base.flash.clone();
        assert!(
            c::boot_go(&mut interrupted, &base.areadesc, None, None, true).success_no_asserts()
        );
        assert!(base.verify_images(&interrupted, 0, 1));
        let mut stop = operation;
        assert!(c::boot_go(
            &mut interrupted,
            &base.areadesc,
            Some(&mut stop),
            None,
            false
        )
        .interrupted());
        assert_eq!(stop, 0, "SDK must stop at the requested revert operation");
        record(
            &mut states,
            &format!("revert-cut-{operation}"),
            &interrupted,
            &base.areadesc,
        );
        assert!(
            c::boot_go(&mut interrupted, &base.areadesc, None, None, true).success_no_asserts()
        );
        assert!(base.verify_images(&interrupted, 0, 0));
        assert_eq!(c::get_security_counter(0), 1);
        record(
            &mut states,
            &format!("revert-resumed-{operation}"),
            &interrupted,
            &base.areadesc,
        );
    }
    assert!(states.len() <= 4 * 1024 * 1024);
    let path = std::env::var_os("NXRS_BOOT_STATE_FIXTURE")
        .expect("explicit private state output required");
    let mut options = std::fs::OpenOptions::new();
    use std::os::unix::fs::OpenOptionsExt;
    options.write(true).create_new(true).mode(0o600);
    let mut output = options.open(path).unwrap();
    output.write_all(b"NXRSBT02").unwrap();
    output.write_all(&(total as u32).to_le_bytes()).unwrap();
    output
        .write_all(&(revert_total as u32).to_le_bytes())
        .unwrap();
    output.write_all(&states).unwrap();
    output.sync_all().unwrap();
}
