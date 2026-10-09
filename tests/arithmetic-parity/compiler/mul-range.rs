#![no_std]

// Ordinary checked multiplication: the compiler discovers the input ranges.
#[no_mangle]
pub extern "C" fn checked_unsigned_halves(a: u32, b: u32, flag: &mut bool) -> i64 {
    let result = (a as i64).checked_mul(b as i64);
    *flag = result.is_none();
    result.unwrap_or(0)
}

#[no_mangle]
pub extern "C" fn checked_masked_halves(a: u64, b: u64, flag: &mut bool) -> i64 {
    let result = ((a & 0xffff_ffff) as i64).checked_mul((b & 0xffff_ffff) as i64);
    *flag = result.is_none();
    result.unwrap_or(0)
}

#[no_mangle]
pub extern "C" fn checked_full_width(a: i64, b: i64, flag: &mut bool) -> i64 {
    let result = a.checked_mul(b);
    *flag = result.is_none();
    result.unwrap_or(0)
}
