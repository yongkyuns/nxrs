//! Reads real SDK observations and executes the exact production Rust predicates.
#![forbid(unsafe_code)]
pub use update::Error;
#[path = "../../hal/update/src/protocol.rs"]
mod protocol;

fn take<'a>(input: &mut &'a [u8], count: usize) -> &'a [u8] {
    let (value, rest) = input.split_at(count);
    *input = rest;
    value
}
fn check(input: &mut &[u8], expected_name: &str, expected: bool) {
    let length = usize::from(take(input, 1)[0]);
    let name = std::str::from_utf8(take(input, length)).unwrap();
    assert_eq!(name, expected_name);
    let slot = u32::from_le_bytes(take(input, 4).try_into().unwrap());
    let primary = take(input, 48).try_into().unwrap();
    let secondary = take(input, 48).try_into().unwrap();
    let scratch = take(input, 48).try_into().unwrap();
    let admitted = protocol::idle_primary(primary, slot)
        && protocol::idle_secondary(secondary).is_ok()
        && protocol::idle_secondary(scratch).is_ok();
    assert_eq!(admitted, expected, "real SDK state {name}");
}
#[test]
fn actual_sdk_boot_states_match_production_staging_policy() {
    let path = std::env::var_os("NXRS_BOOT_STATE_FIXTURE").expect("real SDK states required");
    let bytes = std::fs::read(path).unwrap();
    assert!(bytes.len() <= 4 * 1024 * 1024 + 16);
    let mut input = bytes.as_slice();
    assert_eq!(take(&mut input, 8), b"NXRSBT02");
    let operations = u32::from_le_bytes(take(&mut input, 4).try_into().unwrap());
    assert!((2..=4096).contains(&operations));
    let revert_operations = u32::from_le_bytes(take(&mut input, 4).try_into().unwrap());
    assert!((2..=4096).contains(&revert_operations));
    for (name, admitted) in [
        ("initial", true),
        ("pending", false),
        ("trial", false),
        ("reverted", true),
        ("confirmed", true),
        ("permanent", true),
    ] {
        check(&mut input, name, admitted);
    }
    for operation in 1..=operations {
        check(&mut input, &format!("cut-{operation}"), false);
        check(&mut input, &format!("trial-resumed-{operation}"), false);
        check(&mut input, &format!("reverted-{operation}"), true);
    }
    for operation in 1..=revert_operations {
        check(&mut input, &format!("revert-cut-{operation}"), false);
        check(&mut input, &format!("revert-resumed-{operation}"), true);
    }
    assert!(input.is_empty(), "unknown or duplicate observations");
}
