//! Small compiler reproducer, not a firmware or a timing benchmark.
#![no_std]

#[no_mangle]
pub extern "C" fn checked_zero(a: i64, b: i64) -> i64 {
    a.checked_mul(b).unwrap_or(0)
}

#[no_mangle]
pub extern "C" fn checked_shared_flag(a: i64, b: i64, flag: &mut bool) -> i64 {
    let product = a.checked_mul(b);
    *flag = product.is_none();
    product.unwrap_or(0)
}

#[no_mangle]
pub extern "C" fn checked_nonzero_default(a: i64, b: i64) -> i64 {
    a.checked_mul(b).unwrap_or(1)
}
