//! Private SDK ABI declarations; layouts and enums are translated beside this module.
use std::ffi::{c_char, c_int, c_void};

unsafe extern "C" {
    pub(super) fn tls_attributes_size() -> usize;
    pub(super) fn tls_attributes_align() -> usize;
    pub(super) fn tls_attributes_init(p: *mut c_void);
    pub(super) fn tls_set_type(p: *mut c_void, value: u16);
    pub(super) fn tls_set_bits(p: *mut c_void, value: usize);
    pub(super) fn tls_set_usage(p: *mut c_void, value: u32);
    pub(super) fn tls_set_algorithm(p: *mut c_void, value: u32);
    pub(super) fn tls_set_lifetime(p: *mut c_void, value: u32);
    pub(super) fn tls_p256_type() -> u16;
    pub(super) fn tls_sign_hash_usage() -> u32;
    pub(super) fn tls_ecdsa_sha256() -> u32;
    pub(super) fn tls_volatile_lifetime() -> u32;
    pub(super) fn tls_sha256() -> c_int;
    pub(super) fn tls_entropy_error() -> c_int;
    pub(super) fn tls_pk_size() -> usize;
    pub(super) fn tls_pk_align() -> usize;
    pub(super) fn tls_csr_size() -> usize;
    pub(super) fn tls_csr_align() -> usize;
    pub(super) fn psa_crypto_init() -> c_int;
    pub(super) fn mbedtls_psa_crypto_free();
    pub(super) fn psa_generate_key(attributes: *const c_void, key: *mut u32) -> c_int;
    pub(super) fn psa_destroy_key(key: u32) -> c_int;
    pub(super) fn psa_random_reseed(personalization: *const u8, size: usize) -> c_int;
    pub(super) fn psa_reset_key_attributes(attributes: *mut c_void);
    pub(super) fn mbedtls_pk_init(p: *mut c_void);
    pub(super) fn mbedtls_pk_free(p: *mut c_void);
    pub(super) fn mbedtls_pk_wrap_psa(p: *mut c_void, key: u32) -> c_int;
    pub(super) fn mbedtls_x509write_csr_init(p: *mut c_void);
    pub(super) fn mbedtls_x509write_csr_free(p: *mut c_void);
    pub(super) fn mbedtls_x509write_csr_set_key(p: *mut c_void, key: *mut c_void);
    pub(super) fn tls_csr_set_md_alg(p: *mut c_void, algorithm: c_int);
    pub(super) fn mbedtls_x509write_csr_set_subject_name(
        p: *mut c_void,
        subject: *const c_char,
    ) -> c_int;
    pub(super) fn mbedtls_x509write_csr_pem(p: *mut c_void, output: *mut u8, size: usize) -> c_int;
}

pub(super) type SendCallback = unsafe extern "C" fn(*mut c_void, *const u8, usize) -> c_int;
pub(super) type RecvCallback = unsafe extern "C" fn(*mut c_void, *mut u8, usize) -> c_int;
pub(super) type TimedRecv = unsafe extern "C" fn(*mut c_void, *mut u8, usize, u32) -> c_int;
pub(super) type View = unsafe extern "C" fn(*mut c_void, *mut usize) -> *const u8;
unsafe extern "C" {
    pub(super) fn tls_ssl_size() -> usize;
    pub(super) fn tls_ssl_align() -> usize;
    pub(super) fn tls_config_size() -> usize;
    pub(super) fn tls_config_align() -> usize;
    pub(super) fn tls_crt_size() -> usize;
    pub(super) fn tls_crt_align() -> usize;
    pub(super) fn tls_crl_size() -> usize;
    pub(super) fn tls_crl_align() -> usize;
    pub(super) fn tls_server_endpoint() -> c_int;
    pub(super) fn tls_stream_transport() -> c_int;
    pub(super) fn tls_default_preset() -> c_int;
    pub(super) fn tls_verify_required() -> c_int;
    pub(super) fn tls_version12() -> c_int;
    pub(super) fn tls_version13() -> c_int;
    pub(super) fn tls_ssl_min_version(p: *mut c_void, version: c_int);
    pub(super) fn tls_ssl_max_version(p: *mut c_void, version: c_int);
    pub(super) fn tls_want_read() -> c_int;
    pub(super) fn tls_want_write() -> c_int;
    pub(super) fn tls_peer_closed() -> c_int;
    pub(super) fn tls_io_failure() -> c_int;
    pub(super) fn tls_handshake_over(p: *mut c_void) -> c_int;
    pub(super) fn tls_sign_usage() -> u32;
    pub(super) fn tls_ca_usage() -> u32;
    pub(super) fn tls_ecdsa_signature() -> c_int;
    pub(super) fn tls_crt_pk(p: *mut c_void) -> *mut c_void;
    pub(super) fn tls_crt_ca(p: *mut c_void) -> c_int;
    pub(super) fn tls_crt_usage(p: *mut c_void) -> u32;
    pub(super) fn tls_crt_sig_md(p: *mut c_void) -> c_int;
    pub(super) fn tls_crt_sig_pk(p: *mut c_void) -> c_int;
    pub(super) fn tls_crl_sig_md(p: *mut c_void) -> c_int;
    pub(super) fn tls_crl_sig_pk(p: *mut c_void) -> c_int;
    pub(super) fn tls_crt_from(p: *mut c_void) -> *const c_void;
    pub(super) fn tls_crt_until(p: *mut c_void) -> *const c_void;
    pub(super) fn tls_crl_from(p: *mut c_void) -> *const c_void;
    pub(super) fn tls_crl_until(p: *mut c_void) -> *const c_void;
    pub(super) fn tls_crt_raw(p: *mut c_void, size: *mut usize) -> *const u8;
    pub(super) fn tls_crt_issuer(p: *mut c_void, size: *mut usize) -> *const u8;
    pub(super) fn tls_crt_subject(p: *mut c_void, size: *mut usize) -> *const u8;
    pub(super) fn tls_crt_eku(p: *mut c_void, size: *mut usize) -> *const u8;
    pub(super) fn tls_crt_eku_next(p: *mut c_void) -> *const c_void;
    pub(super) fn tls_crl_raw(p: *mut c_void, size: *mut usize) -> *const u8;
    pub(super) fn tls_crl_issuer(p: *mut c_void, size: *mut usize) -> *const u8;
    pub(super) fn tls_peer_raw(p: *mut c_void, size: *mut usize) -> *const u8;
    pub(super) fn mbedtls_ssl_init(p: *mut c_void);
    pub(super) fn mbedtls_ssl_free(p: *mut c_void);
    pub(super) fn mbedtls_ssl_config_init(p: *mut c_void);
    pub(super) fn mbedtls_ssl_config_free(p: *mut c_void);
    pub(super) fn mbedtls_ssl_config_defaults(
        p: *mut c_void,
        endpoint: c_int,
        transport: c_int,
        preset: c_int,
    ) -> c_int;
    pub(super) fn mbedtls_ssl_conf_authmode(p: *mut c_void, mode: c_int);
    pub(super) fn mbedtls_ssl_conf_ca_chain(p: *mut c_void, issuer: *mut c_void, crl: *mut c_void);
    pub(super) fn mbedtls_ssl_conf_own_cert(
        p: *mut c_void,
        own: *mut c_void,
        key: *mut c_void,
    ) -> c_int;
    pub(super) fn mbedtls_ssl_setup(p: *mut c_void, config: *const c_void) -> c_int;
    pub(super) fn mbedtls_ssl_set_bio(
        p: *mut c_void,
        context: *mut c_void,
        send: Option<SendCallback>,
        recv: Option<RecvCallback>,
        timed: Option<TimedRecv>,
    );
    pub(super) fn mbedtls_ssl_handshake_step(p: *mut c_void) -> c_int;
    pub(super) fn mbedtls_ssl_get_verify_result(p: *const c_void) -> u32;
    pub(super) fn mbedtls_ssl_read(p: *mut c_void, output: *mut u8, size: usize) -> c_int;
    pub(super) fn mbedtls_ssl_write(p: *mut c_void, input: *const u8, size: usize) -> c_int;
    pub(super) fn mbedtls_ssl_close_notify(p: *mut c_void) -> c_int;
    pub(super) fn mbedtls_x509_crt_init(p: *mut c_void);
    pub(super) fn mbedtls_x509_crt_free(p: *mut c_void);
    pub(super) fn mbedtls_x509_crl_init(p: *mut c_void);
    pub(super) fn mbedtls_x509_crl_free(p: *mut c_void);
    pub(super) fn mbedtls_x509_crt_parse_der(
        p: *mut c_void,
        input: *const u8,
        size: usize,
    ) -> c_int;
    pub(super) fn mbedtls_x509_crl_parse_der(
        p: *mut c_void,
        input: *const u8,
        size: usize,
    ) -> c_int;
    pub(super) fn mbedtls_x509_crt_verify(
        p: *mut c_void,
        issuer: *mut c_void,
        crl: *mut c_void,
        name: *const c_char,
        flags: *mut u32,
        verify: Option<unsafe extern "C" fn(*mut c_void, *mut c_void, c_int, *mut u32) -> c_int>,
        context: *mut c_void,
    ) -> c_int;
    pub(super) fn mbedtls_x509_time_is_past(p: *const c_void) -> c_int;
    pub(super) fn mbedtls_x509_time_is_future(p: *const c_void) -> c_int;
    pub(super) fn mbedtls_pk_check_pair(public: *const c_void, private: *const c_void) -> c_int;
}
