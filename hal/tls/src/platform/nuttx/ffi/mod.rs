unsafe extern "C" {
    pub(super) fn tls_rng_admitted() -> bool;
    pub(super) fn tls_entropy_open_flags() -> i32;
}
