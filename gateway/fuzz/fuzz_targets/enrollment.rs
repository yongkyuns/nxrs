#![no_main]
use libfuzzer_sys::fuzz_target;
use nxrs_gateway_control::config::Config;

fuzz_target!(|data: &[u8]| {
    if data.len() > 64 * 1024 {
        return;
    }
    if let Ok(config) = serde_json::from_slice::<Config>(data) {
        if config.validate().is_ok() {
            assert!(config.listen.ip().is_loopback());
            assert!(config.auth_listen.ip().is_loopback());
            assert_ne!(config.listen, config.auth_listen);
            assert!((1..=300).contains(&config.grant_ttl_seconds));
            assert!((1..=1024).contains(&config.max_grants));
            assert!((1..=64).contains(&config.cameras.len()));
            assert!((1..=64).contains(&config.viewers.len()));
        }
    }
});
