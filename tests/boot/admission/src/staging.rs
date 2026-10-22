//! Real crypto through the production Rust staging owner. No activation occurs.
use super::{trusted, Case, Expected, Native, CASES, MODULUS};
use std::num::NonZeroU32;
use std::time::{Duration, Instant};
use update::{Error, Policy, Stager, Update};

fn retire(owner: &mut Stager) {
    owner.begin_shutdown();
    let deadline = Instant::now() + Duration::from_secs(30);
    while !owner.poll_shutdown().expect("qualified retirement") {
        assert!(Instant::now() < deadline, "retirement deadline");
        std::thread::sleep(Duration::from_millis(1));
    }
    assert!(owner.status().retired);
}
fn stage(case: &Case) {
    let policy = Policy::new(&trusted(), NonZeroU32::new(case.floor).unwrap()).unwrap();
    let crypto = Native::new(&MODULUS).expect("real staging crypto root");
    let mut owner = update::open(policy, crypto, Duration::from_secs(30), || {})
        .expect("explicit staging resource");
    let id = owner
        .stage(
            case.image.to_vec().into_boxed_slice(),
            Duration::from_secs(120),
        )
        .expect("bounded stage admission");
    let deadline = Instant::now() + Duration::from_secs(150);
    let mut result = None;
    loop {
        assert!(Instant::now() < deadline, "staging observation deadline");
        match owner.poll() {
            Update::None => std::thread::sleep(Duration::from_millis(1)),
            Update::Completed {
                id: completed,
                result: value,
                quiescent,
            } => {
                assert_eq!(id, completed);
                assert!(quiescent, "native I/O completed before publication");
                assert!(result.replace(value).is_none(), "one completion only");
            }
            Update::Quiescent { id: completed } => {
                assert_eq!(id, completed);
                break;
            }
        }
    }
    let result = result.expect("completion before barrier");
    if case.expected == Expected::Accept {
        let image = result.expect("actual signed image staging and full readback");
        assert!(image.security_counter.get() >= case.floor);
    } else {
        assert_eq!(result, Err(Error::Authentication));
    }
    assert_eq!(owner.status().fault, None);
    assert_eq!(owner.poll(), Update::None);
    retire(&mut owner);
    println!("IMAGE_STAGING_CASE {} passed", case.label);
}

pub(super) fn qualify() -> u8 {
    // All refusal cases run before any accepted image, followed by every real
    // signed positive. The emulator runner independently checks resulting flash.
    for case in CASES
        .iter()
        .filter(|case| case.expected != Expected::Accept)
    {
        stage(case);
    }
    for case in CASES
        .iter()
        .filter(|case| case.expected == Expected::Accept)
    {
        stage(case);
    }
    println!(
        "IMAGE_STAGING_QUALIFIED cases={} activation=false",
        CASES.len()
    );
    0
}
