//! Gateway-owned motion policy and bounded private footage. No camera acquisition or MCU dependency.
#![forbid(unsafe_code)]
pub mod motion;
pub mod backup;
pub mod progress;
pub mod protection;
pub mod store;
pub mod worker;
mod parser;
#[cfg(feature = "fuzzing")]
pub mod fuzzing;

pub fn identifier(value: &str) -> bool {
    (1..=64).contains(&value.len())
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}
pub fn event_id(value: &str) -> bool {
    value.len() == 32
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
pub fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|v| u64::try_from(v.as_millis()).unwrap_or(u64::MAX))
        .unwrap_or(0)
}
