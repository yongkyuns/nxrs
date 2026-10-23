//! Real SDK execution only; never print entropy, private keys or CSR contents.
use super::*;
use std::fs::File;
use std::io::Read;
use std::os::unix::fs::OpenOptionsExt;
use std::sync::{Arc, Mutex};

static SERIAL: Mutex<()> = Mutex::new(());
fn serial() -> std::sync::MutexGuard<'static, ()> {
    SERIAL.lock().unwrap_or_else(|p| p.into_inner())
}
struct OsEntropy(File);
impl OsEntropy {
    fn open() -> Self {
        Self(File::open("/dev/urandom").unwrap())
    }
}
impl Entropy for OsEntropy {
    fn fill(&mut self, output: &mut [u8]) -> Result<(), EntropyUnavailable> {
        self.0.read_exact(output).map_err(|_| EntropyUnavailable)
    }
}
struct Controlled {
    os: OsEntropy,
    fail: Arc<AtomicBool>,
    calls: Arc<AtomicUsize>,
}
impl Entropy for Controlled {
    fn fill(&mut self, output: &mut [u8]) -> Result<(), EntropyUnavailable> {
        self.calls.fetch_add(1, Ordering::Relaxed);
        if self.fail.load(Ordering::Acquire) {
            output[..16].fill(0xa5);
            return Err(EntropyUnavailable);
        }
        self.os.fill(output)
    }
}
unsafe extern "C" {
    fn psa_export_key(key: u32, output: *mut u8, size: usize, length: *mut usize) -> c_int;
    fn psa_generate_random(output: *mut u8, size: usize) -> c_int;
    fn nxrs_di_test_export_usage() -> u32;
    fn nxrs_di_test_entropy_probe(bits: *mut usize, output: *mut u8, size: usize) -> c_int;
}

#[test]
fn actual_key_csr_and_exclusive_owner() {
    let _serial = serial();
    let mut identity = Identity::generate(OsEntropy::open()).unwrap();
    assert!(identity.entropy_calls() > 0);
    assert!(matches!(
        Identity::generate(OsEntropy::open()),
        Err(Error::Busy)
    ));
    let mut output = [0u8; 2048];
    let mut n = identity.request("cam_A", &mut output).unwrap();
    assert!(output[..n].starts_with(b"-----BEGIN CERTIFICATE REQUEST-----\n"));
    assert!(output[n..].iter().all(|b| *b == 0));
    if let Some(directory) = std::env::var_os("NXRS_IDENTITY_PUBLIC_FIXTURE") {
        // This directory is created privately by the qualification driver.
        let directory = std::path::PathBuf::from(directory);
        for name in ["camera-a.csr", "camera-b.csr"] {
            let mut file = std::fs::OpenOptions::new()
                .create_new(true)
                .mode(0o600)
                .write(true)
                .open(directory.join(name))
                .unwrap();
            use std::io::Write;
            file.write_all(&output[..n]).unwrap();
            if name == "camera-a.csr" {
                drop(identity);
                identity = Identity::generate(OsEntropy::open()).unwrap();
                n = identity.request("cam_A", &mut output).unwrap();
            }
        }
    }
    drop(identity);
    assert!(Identity::generate(OsEntropy::open()).is_ok());
}

#[test]
fn actual_psa_policy_denies_private_export_and_control_permits_it() {
    let _serial = serial();
    let identity = Identity::generate(OsEntropy::open()).unwrap();
    let mut output = [0xa5; 256];
    let mut length = 99;
    let denied = identity.sdk(|| unsafe {
        psa_export_key(identity.key, output.as_mut_ptr(), output.len(), &mut length)
    });
    assert_eq!(denied, Err(Error::Crypto));
    assert_eq!(length, 0);
    assert!(output.iter().all(|b| *b == 0xa5));
    let attributes = unsafe {
        Opaque::new(
            nxrs_di_attributes_size(),
            nxrs_di_attributes_align(),
            nxrs_di_attributes_init,
            psa_reset_key_attributes,
        )
        .unwrap()
    };
    unsafe {
        nxrs_di_set_type(attributes.pointer(), nxrs_di_p256_type());
        nxrs_di_set_bits(attributes.pointer(), 256);
        nxrs_di_set_usage(attributes.pointer(), nxrs_di_test_export_usage());
        nxrs_di_set_algorithm(attributes.pointer(), nxrs_di_ecdsa_sha256());
    }
    let mut control_key = 0;
    identity
        .sdk(|| unsafe { psa_generate_key(attributes.pointer(), &mut control_key) })
        .unwrap();
    identity
        .sdk(|| unsafe {
            psa_export_key(control_key, output.as_mut_ptr(), output.len(), &mut length)
        })
        .unwrap();
    assert_eq!(
        length, 32,
        "actual SDK exportable control must reach the export operation"
    );
    output.fill(0);
    assert_eq!(unsafe { psa_destroy_key(control_key) }, 0);
}

#[test]
fn initial_entropy_failure_has_no_fallback_and_fresh_owner_recovers() {
    let _serial = serial();
    let calls = Arc::new(AtomicUsize::new(0));
    let source = Controlled {
        os: OsEntropy::open(),
        fail: Arc::new(AtomicBool::new(true)),
        calls: calls.clone(),
    };
    assert!(matches!(Identity::generate(source), Err(Error::Entropy)));
    assert!(calls.load(Ordering::Relaxed) > 0);
    let mut restored = Identity::generate(OsEntropy::open()).unwrap();
    assert!(restored.request("restored", &mut [0; 2048]).is_ok());
}

#[test]
fn actual_drbg_reseed_failure_clears_request_and_stays_latched() {
    let _serial = serial();
    let fail = Arc::new(AtomicBool::new(false));
    let calls = Arc::new(AtomicUsize::new(0));
    let source = Controlled {
        os: OsEntropy::open(),
        fail: fail.clone(),
        calls: calls.clone(),
    };
    let mut identity = Identity::generate(source).unwrap();
    let initial = calls.load(Ordering::Relaxed);
    fail.store(true, Ordering::Release);
    let mut output = [0xa5; 2048];
    let mut refused = false;
    for _ in 0..2048 {
        if let Err(error) = identity.request("reseed", &mut output) {
            assert_eq!(error, Error::Entropy);
            assert!(output.iter().all(|b| *b == 0));
            refused = true;
            break;
        }
    }
    assert!(
        refused,
        "actual configured HMAC-DRBG must reach the failed reseed"
    );
    assert!(calls.load(Ordering::Relaxed) > initial);
    let failed_calls = calls.load(Ordering::Relaxed);
    fail.store(false, Ordering::Release);
    assert_eq!(
        identity.request("reseed", &mut output),
        Err(Error::Poisoned)
    );
    assert_eq!(calls.load(Ordering::Relaxed), failed_calls);
    drop(identity);
    assert!(Identity::generate(OsEntropy::open())
        .unwrap()
        .request("restored", &mut output)
        .is_ok());
}

#[test]
fn invalid_identity_is_refused_before_sdk_and_capacity_is_bounded() {
    let _serial = serial();
    let mut identity = Identity::generate(OsEntropy::open()).unwrap();
    let initial = identity.entropy_calls();
    let mut output = [0xa5; 2048];
    for name in [
        "",
        "cam,OU=forged",
        "line\nbreak",
        "nul\0value",
        "../cam",
        "π",
    ] {
        assert_eq!(
            identity.request(name, &mut output),
            Err(Error::InvalidInput)
        );
        assert!(output.iter().all(|b| *b == 0));
    }
    assert_eq!(
        identity.request(&"x".repeat(65), &mut output),
        Err(Error::InvalidInput)
    );
    assert_eq!(identity.entropy_calls(), initial);
    assert_eq!(
        identity.request("valid", &mut [0; 128]),
        Err(Error::Capacity)
    );
    assert_eq!(
        identity.request("valid", &mut [0; 4097]),
        Err(Error::Capacity)
    );
    assert!(identity.request("valid", &mut output).is_ok());
}

#[test]
fn foreign_and_late_sdk_callbacks_cannot_borrow_or_reuse_owner_storage() {
    let _serial = serial();
    let mut identity = Identity::generate(OsEntropy::open()).unwrap();
    let initial = identity.entropy_calls();
    std::thread::spawn(|| {
        let mut output = [0xa5; 128];
        let mut bits = 99;
        let result = unsafe {
            mbedtls_platform_get_entropy(0, &mut bits, output.as_mut_ptr(), output.len())
        };
        assert_ne!(result, 0);
        assert_eq!(bits, 0);
        assert!(output.iter().all(|b| *b == 0));
    })
    .join()
    .unwrap();
    assert_eq!(identity.entropy_calls(), initial);
    assert_eq!(
        identity.request("foreign", &mut [0; 2048]),
        Err(Error::Poisoned)
    );
    drop(identity);
    let mut output = [0xa5; 128];
    let mut bits = 99;
    assert_ne!(
        unsafe { mbedtls_platform_get_entropy(0, &mut bits, output.as_mut_ptr(), output.len()) },
        0
    );
    assert_eq!(bits, 0);
    assert!(output.iter().all(|b| *b == 0));
    assert!(Identity::generate(OsEntropy::open()).is_ok());
}

#[test]
fn partial_entropy_error_clears_actual_callback_output_and_estimate() {
    let _serial = serial();
    let fail = Arc::new(AtomicBool::new(false));
    let source = Controlled {
        os: OsEntropy::open(),
        fail: fail.clone(),
        calls: Arc::new(AtomicUsize::new(0)),
    };
    let mut identity = Identity::generate(source).unwrap();
    fail.store(true, Ordering::Release);
    let mut output = [0xa5; 128];
    let mut bits = 99;
    assert_eq!(
        identity.sdk(|| unsafe {
            nxrs_di_test_entropy_probe(&mut bits, output.as_mut_ptr(), output.len())
        }),
        Err(Error::Entropy)
    );
    assert_eq!(bits, 0);
    assert!(output.iter().all(|b| *b == 0));
    fail.store(false, Ordering::Release);
    assert_eq!(
        identity.request("partial", &mut [0; 2048]),
        Err(Error::Poisoned)
    );
}

#[test]
fn explicit_refresh_failure_cannot_continue_with_cached_sdk_randomness() {
    let _serial = serial();
    let fail = Arc::new(AtomicBool::new(false));
    let source = Controlled {
        os: OsEntropy::open(),
        fail: fail.clone(),
        calls: Arc::new(AtomicUsize::new(0)),
    };
    let mut identity = Identity::generate(source).unwrap();
    assert!(identity.refresh_entropy().is_ok());
    fail.store(true, Ordering::Release);
    assert_eq!(identity.refresh_entropy(), Err(Error::Entropy));
    // Actual documented SDK behavior: failed explicit reseed can leave cached
    // DRBG state usable. This control proves the Rust latch is necessary.
    let mut cached = [0; 32];
    assert_eq!(
        unsafe { psa_generate_random(cached.as_mut_ptr(), cached.len()) },
        0
    );
    cached.fill(0);
    assert_eq!(
        identity.request("refresh", &mut [0; 2048]),
        Err(Error::Poisoned)
    );
    fail.store(false, Ordering::Release);
    assert_eq!(identity.refresh_entropy(), Err(Error::Poisoned));
}

#[test]
fn source_retirement_finishes_before_another_sdk_owner_is_admitted() {
    let _serial = serial();
    struct RetiringSource(OsEntropy, Arc<AtomicBool>);
    impl Entropy for RetiringSource {
        fn fill(&mut self, output: &mut [u8]) -> Result<(), EntropyUnavailable> {
            self.0.fill(output)
        }
    }
    impl Drop for RetiringSource {
        fn drop(&mut self) {
            assert!(OWNED.load(Ordering::Acquire));
            assert!(INVOCATION.with(Cell::get).is_null());
            assert!(matches!(
                Identity::generate(OsEntropy::open()),
                Err(Error::Busy)
            ));
            self.1.store(true, Ordering::Release);
        }
    }
    let retired = Arc::new(AtomicBool::new(false));
    let identity = Identity::generate(RetiringSource(OsEntropy::open(), retired.clone())).unwrap();
    drop(identity);
    assert!(retired.load(Ordering::Acquire));
    assert!(!OWNED.load(Ordering::Acquire));
    assert!(Identity::generate(OsEntropy::open()).is_ok());
}
