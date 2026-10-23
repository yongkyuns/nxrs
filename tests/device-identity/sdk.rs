//! Real SDK execution only; never print entropy, private keys or CSR contents.
use super::*;
use std::fs::File;
use std::io::Read;
use std::os::unix::fs::OpenOptionsExt;
use std::sync::{Arc, Mutex};

// Test-only real allocator failure. Only the invoking thread is denied; native
// SDK allocation remains separate, and the harness/independent peer keep running.
thread_local! {
    static DENY_RUST_ALLOCATION: Cell<bool> = const { Cell::new(false) };
    static DENIED_RUST_ALLOCATIONS: Cell<usize> = const { Cell::new(0) };
    static PERMITTED_RUST_ALLOCATIONS: Cell<usize> = const { Cell::new(0) };
}
struct FailureAllocator;
fn allocation_denied() -> bool {
    if DENY_RUST_ALLOCATION.try_with(Cell::get).unwrap_or(false) {
        let permitted = PERMITTED_RUST_ALLOCATIONS.with(Cell::get);
        if permitted > 0 {
            PERMITTED_RUST_ALLOCATIONS.with(|value| value.set(permitted - 1));
            return false;
        }
        let _ = DENIED_RUST_ALLOCATIONS.try_with(|value| value.set(value.get().saturating_add(1)));
        true
    } else {
        false
    }
}
// SAFETY: every permitted operation forwards its exact System allocator
// contract; refused alloc/realloc returns null without freeing old storage.
unsafe impl std::alloc::GlobalAlloc for FailureAllocator {
    unsafe fn alloc(&self, layout: std::alloc::Layout) -> *mut u8 {
        if allocation_denied() {
            ptr::null_mut()
        } else {
            unsafe { std::alloc::GlobalAlloc::alloc(&std::alloc::System, layout) }
        }
    }
    unsafe fn alloc_zeroed(&self, layout: std::alloc::Layout) -> *mut u8 {
        if allocation_denied() {
            ptr::null_mut()
        } else {
            unsafe { std::alloc::GlobalAlloc::alloc_zeroed(&std::alloc::System, layout) }
        }
    }
    unsafe fn realloc(&self, pointer: *mut u8, layout: std::alloc::Layout, size: usize) -> *mut u8 {
        if allocation_denied() {
            ptr::null_mut()
        } else {
            unsafe { std::alloc::GlobalAlloc::realloc(&std::alloc::System, pointer, layout, size) }
        }
    }
    unsafe fn dealloc(&self, pointer: *mut u8, layout: std::alloc::Layout) {
        unsafe { std::alloc::GlobalAlloc::dealloc(&std::alloc::System, pointer, layout) }
    }
}
#[global_allocator]
static FAILURE_ALLOCATOR: FailureAllocator = FailureAllocator;
struct AllocationReset;
impl Drop for AllocationReset {
    fn drop(&mut self) {
        DENY_RUST_ALLOCATION.with(|flag| flag.set(false));
    }
}
fn without_rust_allocations<T>(operation: impl FnOnce() -> T) -> (T, usize) {
    with_rust_allocation_budget(0, operation)
}
fn with_rust_allocation_budget<T>(budget: usize, operation: impl FnOnce() -> T) -> (T, usize) {
    assert!(!DENY_RUST_ALLOCATION.with(Cell::get));
    PERMITTED_RUST_ALLOCATIONS.with(|value| value.set(budget));
    DENIED_RUST_ALLOCATIONS.with(|value| value.set(0));
    DENY_RUST_ALLOCATION.with(|flag| flag.set(true));
    let reset = AllocationReset;
    let result = operation();
    drop(reset);
    (result, DENIED_RUST_ALLOCATIONS.with(Cell::get))
}

fn allocation_counts(name: &str, counts: &[usize]) {
    let root =
        std::path::PathBuf::from(std::env::var_os("NXRS_IDENTITY_CERTIFICATE_FIXTURE").unwrap());
    assert!(counts.iter().all(|value| (1..=64).contains(value)));
    let text = counts
        .iter()
        .map(usize::to_string)
        .collect::<Vec<_>>()
        .join(" ")
        + "\n";
    std::fs::write(root.join(name), text).unwrap();
}

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

struct AllocationSource {
    os: OsEntropy,
    dropped: Arc<AtomicBool>,
}
impl Entropy for AllocationSource {
    fn fill(&mut self, output: &mut [u8]) -> Result<(), EntropyUnavailable> {
        self.os.fill(output)
    }
}
impl Drop for AllocationSource {
    fn drop(&mut self) {
        self.dropped.store(true, Ordering::Release);
    }
}
static ZERO_SIZE_DROPS: AtomicUsize = AtomicUsize::new(0);
#[repr(align(64))]
struct ZeroSizeDrop;
impl Drop for ZeroSizeDrop {
    fn drop(&mut self) {
        ZERO_SIZE_DROPS.fetch_add(1, Ordering::Relaxed);
    }
}
#[test]
fn actual_identity_and_csr_allocation_refusals_retire_sources_and_recover() {
    let _serial = serial();
    let prior = ZERO_SIZE_DROPS.load(Ordering::Relaxed);
    let (zero, denied) = without_rust_allocations(|| try_boxed(ZeroSizeDrop));
    assert_eq!(denied, 0);
    let zero = zero.unwrap();
    assert_eq!(std::mem::size_of::<ZeroSizeDrop>(), 0);
    assert_eq!((&*zero as *const ZeroSizeDrop as usize) % 64, 0);
    drop(zero);
    assert_eq!(ZERO_SIZE_DROPS.load(Ordering::Relaxed), prior + 1);
    let mut identity_failures = 0;
    let mut identity_succeeded = false;
    for budget in 0..64 {
        let dropped = Arc::new(AtomicBool::new(false));
        let source = AllocationSource {
            os: OsEntropy::open(),
            dropped: dropped.clone(),
        };
        let (result, denied) = with_rust_allocation_budget(budget, || Identity::generate(source));
        match result {
            Err(Error::Capacity) => {
                assert_eq!(denied, 1);
                identity_failures += 1;
            }
            Ok(identity) => {
                assert_eq!(denied, 0);
                drop(identity);
                identity_succeeded = true;
            }
            _ => panic!("identity allocation failure category"),
        }
        assert!(dropped.load(Ordering::Acquire));
        assert!(!OWNED.load(Ordering::Acquire));
        if identity_succeeded {
            break;
        }
    }
    assert!(identity_succeeded && identity_failures >= 4);
    let mut identity = Identity::generate(OsEntropy::open()).unwrap();
    let mut output = [0xa5; 2048];
    let mut csr_failures = 0;
    let mut csr_succeeded = false;
    for budget in 0..64 {
        output.fill(0xa5);
        let (result, denied) =
            with_rust_allocation_budget(budget, || identity.request("cam_A", &mut output));
        match result {
            Err(Error::Capacity) => {
                assert_eq!(denied, 1);
                assert!(output.iter().all(|b| *b == 0));
                csr_failures += 1;
            }
            Ok(length) => {
                assert_eq!(denied, 0);
                assert!(output[..length].starts_with(b"-----BEGIN CERTIFICATE REQUEST-----"));
                csr_succeeded = true;
            }
            _ => panic!("CSR allocation failure category"),
        }
        assert!(!identity.failed());
        if csr_succeeded {
            break;
        }
    }
    assert!(csr_succeeded && csr_failures > 0);
    allocation_counts(
        "allocation-identity-csr.counts",
        &[identity_failures, csr_failures],
    );
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

#[test]
fn unsupported_sdk_entropy_request_closes_the_owner_permanently() {
    let _serial = serial();
    let mut identity = Identity::generate(OsEntropy::open()).unwrap();
    let before = identity.entropy_calls();
    let mut output = [0xa5; 257];
    let mut bits = 99;
    let result = identity.sdk(|| unsafe {
        nxrs_di_test_entropy_probe(&mut bits, output.as_mut_ptr(), output.len())
    });
    assert_eq!(result, Err(Error::Entropy));
    assert_eq!(bits, 0);
    assert_eq!(identity.entropy_calls(), before);
    assert_eq!(
        identity.request("unsupported", &mut [0; 2048]),
        Err(Error::Poisoned)
    );
}

fn returned_certificates(identity: &mut Identity<'_>, label: &str) -> std::path::PathBuf {
    let root = std::path::PathBuf::from(
        std::env::var_os("NXRS_IDENTITY_CERTIFICATE_FIXTURE")
            .expect("private certificate fixture directory"),
    );
    let directory = root.join(label);
    std::fs::create_dir(&directory).unwrap();
    let mut request = [0; 2048];
    let length = identity.request("cam_A", &mut request).unwrap();
    std::fs::write(directory.join("request.csr"), &request[..length]).unwrap();
    let python =
        std::env::var_os("NXRS_IDENTITY_QUALIFICATION_PYTHON").expect("pinned private Python");
    let fixture = std::path::Path::new(file!())
        .parent()
        .unwrap()
        .join("certificate_fixture.py");
    let output = std::process::Command::new(python)
        .arg(fixture)
        .arg(&directory)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "actual certificate fixture refused"
    );
    assert!(output
        .stdout
        .ends_with(b"Disposable actual-issuer certificate controls prepared\n"));
    directory
}
fn certificate_policy(directory: &std::path::Path) -> CameraCertificatePolicy<'static> {
    CameraCertificatePolicy {
        issuer_sha256: std::fs::read(directory.join("pin.bin"))
            .unwrap()
            .try_into()
            .unwrap(),
        site_id: "site_a",
        camera_id: "cam_a",
        server_ip: [127, 0, 0, 1],
        trusted_unix_seconds: std::fs::read_to_string(directory.join("now"))
            .unwrap()
            .parse()
            .unwrap(),
    }
}
#[test]
fn returned_certificate_binds_actual_owned_key_scope_issuer_and_time() {
    let _serial = serial();
    let mut identity = Identity::generate(OsEntropy::open()).unwrap();
    let directory = returned_certificates(&mut identity, "binding");
    let root = std::fs::read(directory.join("root.der")).unwrap();
    let valid = std::fs::read(directory.join("valid.der")).unwrap();
    let binding = identity
        .bind_certificate(&valid, &root, certificate_policy(&directory))
        .unwrap();
    assert_eq!(binding.certificate_der().unwrap(), valid);
    assert_eq!(certificate::nxrs_di_trusted_utc(), -1);
    assert_eq!(
        std::thread::spawn(|| certificate::nxrs_di_trusted_utc())
            .join()
            .unwrap(),
        -1
    );
    assert!(binding.remaining_validity().unwrap() <= std::time::Duration::from_secs(300));
    drop(binding);
    let refused = [
        "wrong-uri",
        "wrong-ip",
        "extra-san",
        "duplicate-uri",
        "missing-ip",
        "client-role",
        "extra-role",
        "missing-role",
        "missing-san",
        "ca-leaf",
        "wrong-usage",
        "missing-usage",
        "wrong-key",
        "expired",
        "future",
        "too-long",
        "sha384",
        "bad-signature",
        "trailing",
        "two-certificates",
        "truncated",
    ];
    for name in refused {
        let candidate = std::fs::read(directory.join(format!("{name}.der"))).unwrap();
        assert!(
            identity
                .bind_certificate(&candidate, &root, certificate_policy(&directory))
                .is_err(),
            "accepted {name}"
        );
        assert_eq!(certificate::nxrs_di_trusted_utc(), -1);
    }
    for variant in 0..6 {
        let mut policy = certificate_policy(&directory);
        match variant {
            0 => policy.issuer_sha256[0] ^= 1,
            1 => policy.site_id = "other",
            2 => policy.camera_id = "other",
            3 => policy.server_ip = [127, 0, 0, 2],
            4 => policy.trusted_unix_seconds = 0,
            _ => policy.trusted_unix_seconds = u64::MAX,
        }
        assert!(identity.bind_certificate(&valid, &root, policy).is_err());
    }
    for length in 0..valid.len() {
        assert!(
            identity
                .bind_certificate(&valid[..length], &root, certificate_policy(&directory))
                .is_err(),
            "accepted proper DER prefix"
        );
    }
    assert!(identity
        .bind_certificate(&valid, &root, certificate_policy(&directory))
        .is_ok());
    let mut request = [0; 2048];
    assert!(identity.request("cam_A", &mut request).is_ok());
}
#[test]
fn bound_certificate_expires_monotonically_and_closes_after_entropy_failure() {
    let _serial = serial();
    let failed = Arc::new(AtomicBool::new(false));
    let mut identity = Identity::generate(Controlled {
        os: OsEntropy::open(),
        fail: failed.clone(),
        calls: Arc::new(AtomicUsize::new(0)),
    })
    .unwrap();
    let directory = returned_certificates(&mut identity, "expiry");
    let root = std::fs::read(directory.join("root.der")).unwrap();
    let short = std::fs::read(directory.join("short.der")).unwrap();
    let binding = identity
        .bind_certificate(&short, &root, certificate_policy(&directory))
        .unwrap();
    assert!(binding.certificate_der().is_ok());
    std::thread::sleep(std::time::Duration::from_millis(1100));
    assert_eq!(binding.certificate_der(), Err(Error::Expired));
    drop(binding);
    let valid = std::fs::read(directory.join("valid.der")).unwrap();
    let binding = identity
        .bind_certificate(&valid, &root, certificate_policy(&directory))
        .unwrap();
    failed.store(true, Ordering::Relaxed);
    // The actual SDK reseed consumes the controlled source while the binding
    // borrows its live key owner; this is not an injected binding-state bit.
    assert_eq!(
        identity.sdk(|| unsafe { psa_random_reseed(ptr::null(), 0) }),
        Err(Error::Entropy)
    );
    assert_eq!(binding.certificate_der(), Err(Error::Poisoned));
    assert_eq!(binding.remaining_validity(), Err(Error::Poisoned));
}

fn fixture_bytes(directory: &std::path::Path, name: &str) -> Vec<u8> {
    std::fs::read(directory.join(name)).unwrap()
}
fn revocation_state(directory: &std::path::Path) -> RevocationState {
    RevocationState::from_trusted_floor(certificate_policy(directory).issuer_sha256, 3).unwrap()
}
#[test]
fn authenticated_crl_updates_invalidate_bindings_and_refuse_rollback() {
    let _serial = serial();
    let mut identity = Identity::generate(OsEntropy::open()).unwrap();
    let directory = returned_certificates(&mut identity, "revocation-updates");
    let root = fixture_bytes(&directory, "root.der");
    let leaf = fixture_bytes(&directory, "valid.der");
    let clear = fixture_bytes(&directory, "clear.crl");
    let state = revocation_state(&directory);
    let binding = identity
        .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
        .unwrap();
    assert_eq!(binding.certificate_der().unwrap(), leaf);
    assert_eq!(binding.generation(), Ok(3));
    let remaining = binding.remaining_validity().unwrap();
    let now = certificate_policy(&directory).trusted_unix_seconds;
    assert_eq!(
        state.observe(
            &identity,
            &root,
            &fixture_bytes(&directory, "resigned.crl"),
            now
        ),
        Ok(3)
    );
    assert!(binding.remaining_validity().unwrap() <= remaining);
    assert_eq!(
        state.observe(
            &identity,
            &root,
            &fixture_bytes(&directory, "newer.crl"),
            now
        ),
        Ok(4)
    );
    assert_eq!(binding.certificate_der(), Err(Error::StaleTrust));
    assert_eq!(
        state.observe(&identity, &root, &clear, now),
        Err(Error::Rollback)
    );
    let current = identity
        .bind_current_certificate(
            &leaf,
            &root,
            certificate_policy(&directory),
            &state,
            &fixture_bytes(&directory, "newer.crl"),
        )
        .unwrap();
    // Observe a valid revocation before rejecting the now-revoked leaf. Existing
    // guards must close even though this admission attempt returns an error.
    assert!(matches!(
        identity.bind_current_certificate(
            &leaf,
            &root,
            certificate_policy(&directory),
            &state,
            &fixture_bytes(&directory, "revoked.crl")
        ),
        Err(Error::Revoked)
    ));
    assert_eq!(current.certificate_der(), Err(Error::StaleTrust));
    assert_eq!(
        state.observe(&identity, &root, &clear, now),
        Err(Error::Rollback)
    );
    let floor_state = revocation_state(&directory);
    let current = identity
        .bind_current_certificate(
            &leaf,
            &root,
            certificate_policy(&directory),
            &floor_state,
            &clear,
        )
        .unwrap();
    floor_state.raise_trusted_floor(4).unwrap();
    assert_eq!(current.certificate_der(), Err(Error::StaleTrust));
    assert_eq!(floor_state.raise_trusted_floor(3), Err(Error::Rollback));
    assert_eq!(floor_state.raise_trusted_floor(0), Err(Error::InvalidInput));
    assert_eq!(
        floor_state.raise_trusted_floor(1_000_000_001),
        Err(Error::InvalidInput)
    );
    assert_eq!(
        floor_state.observe(&identity, &root, &clear, now),
        Err(Error::Rollback)
    );
    assert!(RevocationState::from_trusted_floor([0; 32], 0).is_err());
    assert!(RevocationState::from_trusted_floor([0; 32], u64::MAX).is_err());
}
#[test]
fn malformed_and_unauthenticated_crls_cannot_mutate_accepted_state() {
    let _serial = serial();
    let mut identity = Identity::generate(OsEntropy::open()).unwrap();
    let directory = returned_certificates(&mut identity, "revocation-inputs");
    let root = fixture_bytes(&directory, "root.der");
    let leaf = fixture_bytes(&directory, "valid.der");
    let clear = fixture_bytes(&directory, "clear.crl");
    let state = revocation_state(&directory);
    let binding = identity
        .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
        .unwrap();
    let now = certificate_policy(&directory).trusted_unix_seconds;
    for name in [
        "wrong-signer",
        "wrong-aki",
        "wrong-issuer",
        "future",
        "expired",
        "old",
        "long",
        "zero",
        "huge-number",
        "missing-number",
        "delta",
        "unknown",
        "entry-extension",
        "duplicate-serial",
        "too-many",
        "sha384",
        "bad-signature",
        "trailing",
        "chained",
    ] {
        assert!(
            state
                .observe(
                    &identity,
                    &root,
                    &fixture_bytes(&directory, &format!("{name}.crl")),
                    now
                )
                .is_err(),
            "accepted {name}"
        );
        assert_eq!(
            binding.certificate_der().unwrap(),
            leaf,
            "mutated state after {name}"
        );
    }
    for length in 0..clear.len() {
        assert!(
            state
                .observe(&identity, &root, &clear[..length], now)
                .is_err(),
            "accepted prefix {length}"
        );
    }
    for index in 0..clear.len() {
        let mut changed = clear.clone();
        changed[index] ^= 1;
        assert!(
            state.observe(&identity, &root, &changed, now).is_err(),
            "accepted bit mutation {index}"
        );
        assert!(
            binding.certificate_der().is_ok(),
            "poisoned by unsigned mutation {index}"
        );
    }
    for bytes in [
        vec![0; 32769],
        vec![0x30, 0x80, 0, 0],
        vec![0x30, 0x81, 0],
        vec![0x30, 0x82, 0, 128],
        vec![0x3f, 0],
    ] {
        assert!(state.observe(&identity, &root, &bytes, now).is_err());
    }
    assert_eq!(
        state.observe(&identity, &root, &clear, 0),
        Err(Error::InvalidInput)
    );
    assert_eq!(
        state.observe(&identity, &root, &clear, now - 1),
        Err(Error::InvalidInput)
    );
    let mut wrong = root.clone();
    wrong[0] ^= 1;
    assert!(state.observe(&identity, &wrong, &clear, now).is_err());
    let mut policy = certificate_policy(&directory);
    policy.issuer_sha256[0] ^= 1;
    assert!(identity
        .bind_current_certificate(&leaf, &root, policy, &state, &clear)
        .is_err());
    assert!(binding.certificate_der().is_ok());
    // The documented maximum is admitted, while 513 entries above were refused
    // before the native parser could allocate their linked list.
    let maximum = revocation_state(&directory);
    assert_eq!(
        maximum.observe(
            &identity,
            &root,
            &fixture_bytes(&directory, "at-limit.crl"),
            now
        ),
        Ok(3)
    );
}
#[test]
fn crl_expiry_equivocation_and_trust_regression_fail_closed() {
    let _serial = serial();
    let failed = Arc::new(AtomicBool::new(false));
    let mut identity = Identity::generate(Controlled {
        os: OsEntropy::open(),
        fail: failed.clone(),
        calls: Arc::new(AtomicUsize::new(0)),
    })
    .unwrap();
    let directory = returned_certificates(&mut identity, "revocation-lifecycle");
    let root = fixture_bytes(&directory, "root.der");
    let leaf = fixture_bytes(&directory, "valid.der");
    let clear = fixture_bytes(&directory, "clear.crl");
    let now = certificate_policy(&directory).trusted_unix_seconds;
    let state = revocation_state(&directory);
    let short = fixture_bytes(&directory, "short.crl");
    let binding = identity
        .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &short)
        .unwrap();
    assert!(binding.remaining_validity().unwrap() <= std::time::Duration::from_secs(1));
    assert!(binding.certificate_der().is_ok());
    std::thread::sleep(std::time::Duration::from_millis(1100));
    assert_eq!(binding.certificate_der(), Err(Error::Expired));
    assert_eq!(state.observe(&identity, &root, &short, now), Ok(3));
    assert_eq!(binding.certificate_der(), Err(Error::Expired));
    assert!(matches!(
        identity.bind_current_certificate(
            &leaf,
            &root,
            certificate_policy(&directory),
            &state,
            &short
        ),
        Err(Error::Expired)
    ));
    for update in ["conflict", "removed", "time-regression"] {
        let state = revocation_state(&directory);
        let start = if update == "conflict" {
            &clear
        } else {
            &fixture_bytes(&directory, "revoked.crl")
        };
        state.observe(&identity, &root, start, now).unwrap();
        // Establish a live guard where the starting CRL admits this key.
        let binding = if update == "conflict" {
            Some(
                identity
                    .bind_current_certificate(
                        &leaf,
                        &root,
                        certificate_policy(&directory),
                        &state,
                        &clear,
                    )
                    .unwrap(),
            )
        } else {
            None
        };
        assert_eq!(
            state.observe(
                &identity,
                &root,
                &fixture_bytes(&directory, &format!("{update}.crl")),
                now
            ),
            Err(Error::Poisoned)
        );
        if let Some(binding) = binding {
            assert_eq!(binding.certificate_der(), Err(Error::Poisoned));
        }
        assert_eq!(
            state.observe(&identity, &root, start, now),
            Err(Error::Poisoned)
        );
        assert_eq!(state.raise_trusted_floor(8), Err(Error::Poisoned));
    }
    let state = revocation_state(&directory);
    let binding = identity
        .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
        .unwrap();
    failed.store(true, Ordering::Relaxed);
    assert_eq!(
        identity.sdk(|| unsafe { psa_random_reseed(ptr::null(), 0) }),
        Err(Error::Entropy)
    );
    assert_eq!(binding.certificate_der(), Err(Error::Poisoned));
    assert_eq!(
        state.observe(&identity, &root, &clear, now),
        Err(Error::Poisoned)
    );
}

fn tls_controls(identity: &mut Identity<'_>, name: &str) -> std::path::PathBuf {
    let parent =
        std::path::PathBuf::from(std::env::var_os("NXRS_IDENTITY_CERTIFICATE_FIXTURE").unwrap());
    let directory = parent.join(name);
    std::fs::create_dir(&directory).unwrap();
    let mut csr = [0; 2048];
    let length = identity.request("cam_A", &mut csr).unwrap();
    std::fs::write(directory.join("request.csr"), &csr[..length]).unwrap();
    let python = std::env::var_os("NXRS_IDENTITY_QUALIFICATION_PYTHON").unwrap();
    let fixture = std::path::Path::new(file!())
        .parent()
        .unwrap()
        .join("tls_fixture.py");
    let output = std::process::Command::new(python)
        .arg(fixture)
        .arg("prepare")
        .arg(&directory)
        .output()
        .unwrap();
    std::fs::write(directory.join("prepare-private.err"), &output.stderr).unwrap();
    assert!(output.status.success(), "private TLS authority refused");
    assert!(output
        .stdout
        .ends_with(b"Disposable actual TLS client/issuer controls prepared\n"));
    directory
}
fn tls_policy(directory: &std::path::Path) -> TlsPolicy<'static> {
    TlsPolicy {
        camera: certificate_policy(directory),
        gateway_id: "gw_a",
        handshake_timeout: std::time::Duration::from_secs(10),
        record_timeout: std::time::Duration::from_secs(3),
    }
}
struct TlsClient(std::process::Child);
impl std::ops::Deref for TlsClient {
    type Target = std::process::Child;
    fn deref(&self) -> &Self::Target {
        &self.0
    }
}
impl std::ops::DerefMut for TlsClient {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.0
    }
}
impl Drop for TlsClient {
    fn drop(&mut self) {
        if matches!(self.0.try_wait(), Ok(None)) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}
fn tls_client(directory: &std::path::Path, port: u16, label: &str, version: &str) -> TlsClient {
    let python = std::env::var_os("NXRS_IDENTITY_QUALIFICATION_PYTHON").unwrap();
    let fixture = std::path::Path::new(file!())
        .parent()
        .unwrap()
        .join("tls_fixture.py");
    TlsClient(
        std::process::Command::new(python)
            .arg(fixture)
            .arg("client")
            .arg(directory)
            .args([
                "--port",
                &port.to_string(),
                "--case",
                label,
                "--version",
                version,
            ])
            .env_remove("SSLKEYLOGFILE")
            .stdout(
                std::fs::File::create(
                    directory.join(format!("client-{label}-{version}-private.out")),
                )
                .unwrap(),
            )
            .stderr(
                std::fs::File::create(
                    directory.join(format!("client-{label}-{version}-private.err")),
                )
                .unwrap(),
            )
            .spawn()
            .unwrap(),
    )
}
fn accepted(listener: &std::net::TcpListener) -> std::net::TcpStream {
    listener.set_nonblocking(true).unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
    loop {
        match listener.accept() {
            Ok((socket, _)) => return socket,
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                assert!(
                    std::time::Instant::now() < deadline,
                    "test peer did not connect"
                );
                std::thread::sleep(std::time::Duration::from_millis(1));
            }
            Err(error) => panic!("test accept {error}"),
        }
    }
}
fn wait_client(client: &mut TlsClient, expected: bool) {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
    loop {
        if let Some(status) = client.try_wait().unwrap() {
            assert_eq!(status.success(), expected, "actual TLS client outcome");
            return;
        }
        if std::time::Instant::now() >= deadline {
            client.kill().unwrap();
            client.wait().unwrap();
            panic!("test client deadline");
        }
        std::thread::sleep(std::time::Duration::from_millis(1));
    }
}
#[test]
fn actual_tls_server_accepts_scoped_client_in_tls12_and_tls13() {
    use std::io::{Read, Write};
    let _serial = serial();
    let mut identity = Identity::generate(OsEntropy::open()).unwrap();
    let directory = tls_controls(&mut identity, "tls-positive");
    let root = fixture_bytes(&directory, "root.der");
    let leaf = fixture_bytes(&directory, "server.der");
    let crl = fixture_bytes(&directory, "clear.crl");
    for version in ["12", "13"] {
        let state =
            RevocationState::from_trusted_floor(certificate_policy(&directory).issuer_sha256, 1)
                .unwrap();
        let bound = identity
            .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &crl)
            .unwrap();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let mut client = tls_client(
            &directory,
            listener.local_addr().unwrap().port(),
            "valid",
            version,
        );
        let socket = accepted(&listener);
        let mut session = bound
            .tls_server_tcp(
                socket,
                &root,
                tls_policy(&directory),
                TlsCancellation::default(),
            )
            .unwrap();
        let started = std::time::Instant::now();
        loop {
            let result = session.poll_handshake();
            if let Err(error) = result {
                panic!("actual TLS handshake {version} refused: {error:?}");
            }
            if result.unwrap() == TlsProgress::Established {
                break;
            }
            assert!(started.elapsed() < std::time::Duration::from_secs(10));
            std::thread::sleep(std::time::Duration::from_millis(1));
        }
        assert_eq!(
            session.negotiated_version().unwrap(),
            Some(if version == "12" {
                TlsVersion::V1_2
            } else {
                TlsVersion::V1_3
            })
        );
        let mut request = Vec::new();
        let mut bytes = [0; 128];
        while !request.ends_with(b"\r\n\r\n") {
            match session.read(&mut bytes) {
                Ok(n) if n > 0 => request.extend_from_slice(&bytes[..n]),
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(std::time::Duration::from_millis(1))
                }
                other => panic!("actual TLS request {other:?}"),
            }
            assert!(
                request.len() <= 1024 && started.elapsed() < std::time::Duration::from_secs(10)
            );
        }
        assert_eq!(request, b"GET /stream HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n");
        let response = b"nxrs actual SDK mutual TLS response\n";
        let mut sent = 0;
        while sent < response.len() {
            match session.write(&response[sent..]) {
                Ok(n) => {
                    assert!(n > 0);
                    sent += n;
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(std::time::Duration::from_millis(1))
                }
                other => panic!("actual TLS response {other:?}"),
            }
            assert!(started.elapsed() < std::time::Duration::from_secs(10));
        }
        wait_client(&mut client, true);
        session.close();
        assert_eq!(session.poll_handshake(), Err(TlsError::Closed));
        assert_eq!(
            session.read(&mut bytes).unwrap_err().kind(),
            std::io::ErrorKind::UnexpectedEof
        );
    }
}

fn handshake<T: std::io::Read + std::io::Write>(
    session: &mut TlsServerSession<'_, '_, '_, T>,
) -> Result<(), TlsError> {
    let started = std::time::Instant::now();
    loop {
        if session.poll_handshake()? == TlsProgress::Established {
            return Ok(());
        }
        assert!(
            started.elapsed() < std::time::Duration::from_secs(12),
            "test handshake outer deadline"
        );
        std::thread::sleep(std::time::Duration::from_millis(1));
    }
}
#[test]
fn actual_tls_server_refuses_wrong_peer_roles_scope_dates_and_issuers() {
    let _serial = serial();
    let mut identity = Identity::generate(OsEntropy::open()).unwrap();
    let directory = tls_controls(&mut identity, "tls-refusals");
    let root = fixture_bytes(&directory, "root.der");
    let leaf = fixture_bytes(&directory, "server.der");
    let crl = fixture_bytes(&directory, "clear.crl");
    let state =
        RevocationState::from_trusted_floor(certificate_policy(&directory).issuer_sha256, 1)
            .unwrap();
    for label in [
        "no-certificate",
        "wrong-camera",
        "wrong-gateway",
        "wrong-site",
        "wrong-role",
        "missing-role",
        "extra-role",
        "extra-san",
        "missing-san",
        "ca-leaf",
        "expired",
        "future",
        "too-long",
        "sha384",
        "bad-signature",
        "unknown-ca",
    ] {
        for version in ["12", "13"] {
            let bound = identity
                .bind_current_certificate(
                    &leaf,
                    &root,
                    certificate_policy(&directory),
                    &state,
                    &crl,
                )
                .unwrap();
            let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            let mut client = tls_client(
                &directory,
                listener.local_addr().unwrap().port(),
                label,
                version,
            );
            let socket = accepted(&listener);
            let mut session = bound
                .tls_server_tcp(
                    socket,
                    &root,
                    tls_policy(&directory),
                    TlsCancellation::default(),
                )
                .unwrap();
            assert!(
                handshake(&mut session).is_err(),
                "accepted {label} TLS{version}"
            );
            assert!(!identity.failed(), "bad peer poisoned a healthy owner");
            wait_client(&mut client, false);
            assert!(std::fs::read_to_string(
                directory.join(format!("client-{label}-{version}-private.out"))
            )
            .unwrap()
            .contains("TLS client connected"));
        }
    }
    let state =
        RevocationState::from_trusted_floor(certificate_policy(&directory).issuer_sha256, 1)
            .unwrap();
    let bound = identity
        .bind_current_certificate(
            &leaf,
            &root,
            certificate_policy(&directory),
            &state,
            &fixture_bytes(&directory, "revoked.crl"),
        )
        .unwrap();
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let mut client = tls_client(
        &directory,
        listener.local_addr().unwrap().port(),
        "valid",
        "13",
    );
    let socket = accepted(&listener);
    let mut session = bound
        .tls_server_tcp(
            socket,
            &root,
            tls_policy(&directory),
            TlsCancellation::default(),
        )
        .unwrap();
    assert_eq!(
        handshake(&mut session),
        Err(TlsError::Identity(Error::Revoked))
    );
    wait_client(&mut client, false);
}
#[test]
fn actual_tls_server_retires_active_sessions_after_revocation_entropy_loss_and_expiry() {
    use std::io::Write;
    let _serial = serial();
    let failed = Arc::new(AtomicBool::new(false));
    let mut identity = Identity::generate(Controlled {
        os: OsEntropy::open(),
        fail: failed.clone(),
        calls: Arc::new(AtomicUsize::new(0)),
    })
    .unwrap();
    let directory = tls_controls(&mut identity, "tls-retirement");
    let root = fixture_bytes(&directory, "root.der");
    let leaf = fixture_bytes(&directory, "server.der");
    let clear = fixture_bytes(&directory, "clear.crl");
    for mode in ["revocation", "floor", "expiry", "cancel", "entropy"] {
        let state =
            RevocationState::from_trusted_floor(certificate_policy(&directory).issuer_sha256, 1)
                .unwrap();
        let bound = identity
            .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
            .unwrap();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let label = if mode == "expiry" { "short" } else { "valid" };
        let mut client = tls_client(
            &directory,
            listener.local_addr().unwrap().port(),
            label,
            "13",
        );
        let socket = accepted(&listener);
        let cancellation = TlsCancellation::default();
        let mut session = bound
            .tls_server_tcp(socket, &root, tls_policy(&directory), cancellation.clone())
            .unwrap();
        handshake(&mut session).unwrap();
        assert!(session.remaining_validity().unwrap() > std::time::Duration::ZERO);
        let expected = match mode {
            "revocation" => {
                state
                    .observe(
                        &identity,
                        &root,
                        &fixture_bytes(&directory, "revoked.crl"),
                        certificate_policy(&directory).trusted_unix_seconds,
                    )
                    .unwrap();
                TlsError::Identity(Error::StaleTrust)
            }
            "floor" => {
                state.raise_trusted_floor(5).unwrap();
                TlsError::Identity(Error::StaleTrust)
            }
            "expiry" => {
                std::thread::sleep(
                    session.remaining_validity().unwrap() + std::time::Duration::from_millis(20),
                );
                TlsError::Identity(Error::Expired)
            }
            "cancel" => {
                cancellation.cancel();
                TlsError::Cancelled
            }
            _ => {
                failed.store(true, Ordering::Release);
                assert_eq!(identity.refresh_entropy(), Err(Error::Entropy));
                TlsError::Identity(Error::Poisoned)
            }
        };
        let error = session.write(b"plaintext must remain denied").unwrap_err();
        // Standard I/O reports the stable kind without allocating a boxed cause;
        // the typed session API must retain the exact sticky security failure.
        assert_eq!(error.kind(), std::io::Error::from(expected).kind());
        assert_eq!(session.poll_handshake(), Err(expected));
        let mut bytes = [0xa5; 128];
        assert!(session.read(&mut bytes).is_err());
        assert!(bytes.iter().all(|b| *b == 0));
        wait_client(&mut client, false);
        if mode != "entropy" {
            assert!(!identity.failed());
        }
    }
    assert!(identity.failed());
    drop(identity);
    assert!(Identity::generate(OsEntropy::open()).is_ok());
}
struct Fragmented {
    socket: std::net::TcpStream,
    hold: Arc<AtomicBool>,
    chunk: Arc<AtomicUsize>,
    pending: Arc<AtomicBool>,
    read_bytes: Arc<AtomicUsize>,
}
impl std::io::Read for Fragmented {
    fn read(&mut self, bytes: &mut [u8]) -> std::io::Result<usize> {
        let length = bytes.len().min(self.chunk.load(Ordering::Acquire));
        let n = self.socket.read(&mut bytes[..length])?;
        self.read_bytes.fetch_add(n, Ordering::Relaxed);
        Ok(n)
    }
}
impl std::io::Write for Fragmented {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if self.hold.load(Ordering::Acquire) || self.pending.swap(false, Ordering::AcqRel) {
            return Err(std::io::ErrorKind::WouldBlock.into());
        }
        let length = bytes.len().min(self.chunk.load(Ordering::Acquire));
        let n = self.socket.write(&bytes[..length])?;
        self.pending.store(true, Ordering::Release);
        Ok(n)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn owned_socket_pair() -> (std::net::TcpStream, std::net::TcpStream) {
    let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let peer = std::net::TcpStream::connect(l.local_addr().unwrap()).unwrap();
    (accepted(&l), peer)
}
#[test]
fn actual_certificate_and_crl_allocation_refusals_preserve_healthy_trust() {
    let _serial = serial();
    let mut identity = Identity::generate(OsEntropy::open()).unwrap();
    let directory = tls_controls(&mut identity, "allocation-certificate-crl");
    let root = fixture_bytes(&directory, "root.der");
    let leaf = fixture_bytes(&directory, "server.der");
    let clear = fixture_bytes(&directory, "clear.crl");
    let revoked = fixture_bytes(&directory, "revoked.crl");
    let mut certificate_failures = 0;
    let mut certificate_succeeded = false;
    for budget in 0..64 {
        let policy = certificate_policy(&directory);
        let (result, denied) =
            with_rust_allocation_budget(budget, || identity.bind_certificate(&leaf, &root, policy));
        match result {
            Err(Error::Capacity) => {
                assert_eq!(denied, 1);
                certificate_failures += 1;
            }
            Ok(bound) => {
                assert_eq!(denied, 0);
                assert_eq!(bound.certificate_der().unwrap(), leaf);
                certificate_succeeded = true;
            }
            _ => panic!("certificate allocation failure category"),
        }
        assert!(!identity.failed());
        if certificate_succeeded {
            break;
        }
    }
    assert!(certificate_succeeded && certificate_failures > 0);
    let mut crl_failures = 0;
    let mut crl_succeeded = false;
    for budget in 0..64 {
        let state = revocation_state(&directory);
        let generation = state
            .observe(
                &identity,
                &root,
                &clear,
                certificate_policy(&directory).trusted_unix_seconds,
            )
            .unwrap();
        let bound = identity
            .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
            .unwrap();
        let utc = certificate_policy(&directory).trusted_unix_seconds;
        let (result, denied) =
            with_rust_allocation_budget(budget, || state.observe(&identity, &root, &revoked, utc));
        match result {
            Err(Error::Capacity) => {
                assert_eq!(denied, 1);
                assert_eq!(bound.certificate_der().unwrap(), leaf);
                crl_failures += 1;
            }
            Ok(newer) => {
                assert_eq!(denied, 0);
                assert!(newer > generation);
                assert_eq!(bound.certificate_der(), Err(Error::StaleTrust));
                crl_succeeded = true;
            }
            _ => panic!("CRL allocation failure category"),
        }
        assert!(!identity.failed());
        if crl_succeeded {
            break;
        }
    }
    assert!(crl_succeeded && crl_failures > 0);
    allocation_counts(
        "allocation-certificate-crl.counts",
        &[certificate_failures, crl_failures],
    );
}

#[test]
fn actual_tls_constructor_allocation_refusals_retire_io_before_lease() {
    let _serial = serial();
    let mut identity = Identity::generate(OsEntropy::open()).unwrap();
    let directory = tls_controls(&mut identity, "allocation-tls-constructor");
    let root = fixture_bytes(&directory, "root.der");
    let leaf = fixture_bytes(&directory, "server.der");
    let clear = fixture_bytes(&directory, "clear.crl");
    let state = revocation_state(&directory);
    let mut failures = 0;
    let mut succeeded = false;
    for budget in 0..64 {
        let bound = identity
            .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
            .unwrap();
        let (socket, _peer) = owned_socket_pair();
        socket.set_nonblocking(true).unwrap();
        let dropped = Cell::new(false);
        let cancellation = TlsCancellation::default();
        let policy = tls_policy(&directory);
        let (result, denied) = with_rust_allocation_budget(budget, || {
            bound.tls_server(
                AllocationIo {
                    socket,
                    owner: &identity,
                    dropped: &dropped,
                },
                &root,
                policy,
                cancellation,
            )
        });
        match result {
            Err(TlsError::ResourceLimit) => {
                assert_eq!(denied, 1);
                failures += 1;
            }
            Ok(mut session) => {
                assert_eq!(denied, 0);
                session.close();
                succeeded = true;
            }
            _ => panic!("TLS constructor allocation failure category"),
        }
        assert!(dropped.get());
        assert!(!identity.tls_owned.get());
        assert!(!identity.failed());
        if succeeded {
            break;
        }
    }
    assert!(succeeded && failures > 0);
    allocation_counts("allocation-tls-constructor.counts", &[failures]);
}

#[test]
fn actual_tls_peer_callback_allocation_refusals_close_without_media_and_recover() {
    use std::io::Write;
    let _serial = serial();
    let mut identity = Identity::generate(OsEntropy::open()).unwrap();
    let directory = tls_controls(&mut identity, "allocation-tls-peer");
    let root = fixture_bytes(&directory, "root.der");
    let leaf = fixture_bytes(&directory, "server.der");
    let clear = fixture_bytes(&directory, "clear.crl");
    let state = revocation_state(&directory);
    let mut failures = 0;
    let mut succeeded = false;
    for budget in 0..64 {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let mut client = tls_client(
            &directory,
            listener.local_addr().unwrap().port(),
            "valid",
            "13",
        );
        let socket = accepted(&listener);
        socket.set_nonblocking(true).unwrap();
        let dropped = Cell::new(false);
        let bound = identity
            .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
            .unwrap();
        let mut session = bound
            .tls_server(
                AllocationIo {
                    socket,
                    owner: &identity,
                    dropped: &dropped,
                },
                &root,
                tls_policy(&directory),
                TlsCancellation::default(),
            )
            .unwrap();
        let (result, denied) = with_rust_allocation_budget(budget, || handshake(&mut session));
        match result {
            Err(TlsError::ResourceLimit) => {
                assert_eq!(denied, 1);
                assert!(dropped.get());
                assert!(!identity.tls_owned.get());
                assert_eq!(session.poll_handshake(), Err(TlsError::ResourceLimit));
                assert_eq!(
                    session.write(b"must remain denied").unwrap_err().kind(),
                    std::io::ErrorKind::OutOfMemory
                );
                wait_client(&mut client, false);
                failures += 1;
            }
            Ok(()) => {
                assert_eq!(denied, 0);
                let response = b"nxrs actual SDK mutual TLS response\n";
                let deadline = std::time::Instant::now() + std::time::Duration::from_secs(3);
                loop {
                    match session.write(response) {
                        Ok(length) => {
                            assert_eq!(length, response.len());
                            break;
                        }
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {}
                        _ => panic!("peer allocation recovery write"),
                    }
                    assert!(std::time::Instant::now() < deadline);
                    std::thread::sleep(std::time::Duration::from_millis(1));
                }
                wait_client(&mut client, true);
                session.close();
                succeeded = true;
            }
            _ => panic!("TLS peer allocation failure category"),
        }
        assert!(dropped.get());
        assert!(!identity.tls_owned.get());
        assert!(!identity.failed());
        if succeeded {
            break;
        }
    }
    assert!(succeeded && failures > 0);
    allocation_counts("allocation-tls-peer.counts", &[failures]);
}

#[test]
fn actual_tls_io_errors_need_no_rust_allocation() {
    use std::io::ErrorKind;
    let cases = [
        (TlsError::Busy, ErrorKind::WouldBlock),
        (TlsError::ResourceLimit, ErrorKind::OutOfMemory),
        (
            TlsError::Transport(ErrorKind::OutOfMemory),
            ErrorKind::OutOfMemory,
        ),
        (TlsError::Cancelled, ErrorKind::ConnectionAborted),
        (TlsError::Deadline, ErrorKind::TimedOut),
        (TlsError::Closed, ErrorKind::UnexpectedEof),
        (TlsError::InvalidInput, ErrorKind::InvalidInput),
        (TlsError::Protocol, ErrorKind::InvalidData),
        (
            TlsError::Identity(Error::Entropy),
            ErrorKind::PermissionDenied,
        ),
    ];
    for (error, expected) in cases {
        let (converted, denied) = without_rust_allocations(|| std::io::Error::from(error));
        assert_eq!(converted.kind(), expected);
        assert_eq!(denied, 0, "error conversion attempted an allocation");
    }
    // Positive control proves the real allocator is refusing, rather than merely
    // returning an injected error from a TLS helper.
    let (allocation, denied) =
        without_rust_allocations(|| Vec::<u8>::new().try_reserve_exact(4096));
    assert!(allocation.is_err());
    assert_eq!(denied, 1);
}

struct AllocationIo<'key, 'entropy, 'observed> {
    socket: std::net::TcpStream,
    owner: &'key Identity<'entropy>,
    dropped: &'observed Cell<bool>,
}
impl std::io::Read for AllocationIo<'_, '_, '_> {
    fn read(&mut self, output: &mut [u8]) -> std::io::Result<usize> {
        self.socket.read(output)
    }
}
impl std::io::Write for AllocationIo<'_, '_, '_> {
    fn write(&mut self, input: &[u8]) -> std::io::Result<usize> {
        self.socket.write(input)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.socket.flush()
    }
}
impl Drop for AllocationIo<'_, '_, '_> {
    fn drop(&mut self) {
        assert!(
            self.owner.tls_owned.get(),
            "I/O retired after TLS lease release"
        );
        self.dropped.set(true);
    }
}
#[test]
fn actual_tls_write_allocation_failure_retires_transport_and_fresh_session_recovers() {
    use std::io::Write;
    let _serial = serial();
    let mut identity = Identity::generate(OsEntropy::open()).unwrap();
    let directory = tls_controls(&mut identity, "tls-write-allocation");
    let root = fixture_bytes(&directory, "root.der");
    let leaf = fixture_bytes(&directory, "server.der");
    let clear = fixture_bytes(&directory, "clear.crl");
    let state = revocation_state(&directory);
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let mut client = tls_client(
        &directory,
        listener.local_addr().unwrap().port(),
        "valid",
        "13",
    );
    let socket = accepted(&listener);
    socket.set_nonblocking(true).unwrap();
    let dropped = Cell::new(false);
    let bound = identity
        .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
        .unwrap();
    let mut session = bound
        .tls_server(
            AllocationIo {
                socket,
                owner: &identity,
                dropped: &dropped,
            },
            &root,
            tls_policy(&directory),
            TlsCancellation::default(),
        )
        .unwrap();
    handshake(&mut session).unwrap();
    let response = b"nxrs actual SDK mutual TLS response\n";
    let (result, denied) = without_rust_allocations(|| session.write(response));
    assert_eq!(result.unwrap_err().kind(), std::io::ErrorKind::OutOfMemory);
    assert_eq!(denied, 1, "staging failure must not allocate another error");
    assert!(dropped.get());
    assert!(!identity.tls_owned.get());
    assert_eq!(session.poll_handshake(), Err(TlsError::ResourceLimit));
    assert_eq!(
        session.write(response).unwrap_err().kind(),
        std::io::ErrorKind::OutOfMemory
    );
    wait_client(&mut client, false); // Independent peer receives no application response.
    assert!(!identity.failed());
    // Keep the failed session alive: its later Drop must not release this new
    // owner's lease or transport. Only explicit new admission can recover.
    let bound = identity
        .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
        .unwrap();
    let mut client = tls_client(
        &directory,
        listener.local_addr().unwrap().port(),
        "valid",
        "13",
    );
    let mut recovered = bound
        .tls_server_tcp(
            accepted(&listener),
            &root,
            tls_policy(&directory),
            TlsCancellation::default(),
        )
        .unwrap();
    handshake(&mut recovered).unwrap();
    drop(session);
    assert!(identity.tls_owned.get());
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(3);
    loop {
        match recovered.write(response) {
            Ok(length) => {
                assert_eq!(length, response.len());
                break;
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {}
            other => panic!("fresh allocation recovery: {other:?}"),
        }
        assert!(std::time::Instant::now() < deadline);
        std::thread::sleep(std::time::Duration::from_millis(1));
    }
    wait_client(&mut client, true);
    recovered.close();
    assert!(!identity.tls_owned.get());
}

#[test]
fn actual_tls_server_bounds_deadlines_cancellation_and_nonblocking_write_retries() {
    use std::io::Write;
    let _serial = serial();
    let mut identity = Identity::generate(OsEntropy::open()).unwrap();
    let directory = tls_controls(&mut identity, "tls-bounds");
    let root = fixture_bytes(&directory, "root.der");
    let leaf = fixture_bytes(&directory, "server.der");
    let clear = fixture_bytes(&directory, "clear.crl");
    let state =
        RevocationState::from_trusted_floor(certificate_policy(&directory).issuer_sha256, 1)
            .unwrap();
    for cancelled in [false, true] {
        let bound = identity
            .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
            .unwrap();
        let (socket, mut peer) = owned_socket_pair();
        let mut policy = tls_policy(&directory);
        policy.handshake_timeout = std::time::Duration::from_millis(30);
        let cancellation = TlsCancellation::default();
        let mut session = bound
            .tls_server_tcp(socket, &root, policy, cancellation.clone())
            .unwrap();
        if cancelled {
            cancellation.cancel();
            assert_eq!(session.poll_handshake(), Err(TlsError::Cancelled));
        } else {
            assert!(matches!(
                session.poll_handshake(),
                Ok(TlsProgress::Progress | TlsProgress::WantRead)
            ));
            std::thread::sleep(std::time::Duration::from_millis(40));
            assert_eq!(session.poll_handshake(), Err(TlsError::Deadline));
        }
        peer.set_read_timeout(Some(std::time::Duration::from_secs(1)))
            .unwrap();
        let mut byte = [0];
        assert_eq!(peer.read(&mut byte).unwrap(), 0);
    }
    for mode in ["resume", "changed", "deadline"] {
        let bound = identity
            .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
            .unwrap();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let mut client = tls_client(
            &directory,
            listener.local_addr().unwrap().port(),
            "valid",
            "13",
        );
        let socket = accepted(&listener);
        socket.set_nonblocking(true).unwrap();
        let hold = Arc::new(AtomicBool::new(false));
        let chunk = Arc::new(AtomicUsize::new(7));
        let stream = Fragmented {
            socket,
            hold: hold.clone(),
            chunk: chunk.clone(),
            pending: Arc::new(AtomicBool::new(false)),
            read_bytes: Arc::new(AtomicUsize::new(0)),
        };
        let mut policy = tls_policy(&directory);
        if mode == "deadline" {
            policy.record_timeout = std::time::Duration::from_millis(30);
        }
        let mut session = bound
            .tls_server(stream, &root, policy, TlsCancellation::default())
            .unwrap();
        handshake(&mut session).unwrap();
        let response = b"nxrs actual SDK mutual TLS response\n";
        hold.store(true, Ordering::Release);
        assert_eq!(
            session.write(response).unwrap_err().kind(),
            std::io::ErrorKind::WouldBlock
        );
        if mode == "changed" {
            assert_eq!(
                session.write(b"changed retry").unwrap_err().kind(),
                std::io::ErrorKind::InvalidInput
            );
            wait_client(&mut client, false);
        } else if mode == "deadline" {
            std::thread::sleep(std::time::Duration::from_millis(40));
            assert_eq!(
                session.write(response).unwrap_err().kind(),
                std::io::ErrorKind::TimedOut
            );
            wait_client(&mut client, false);
        } else {
            hold.store(false, Ordering::Release);
            let started = std::time::Instant::now();
            loop {
                match session.write(response) {
                    Ok(n) => {
                        assert_eq!(n, response.len());
                        break;
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                    other => panic!("fragmented record {other:?}"),
                }
                assert!(started.elapsed() < std::time::Duration::from_secs(3));
                std::thread::sleep(std::time::Duration::from_millis(1));
            }
            wait_client(&mut client, true);
        }
        session.close();
        assert!(!identity.failed());
    }
}

struct MemoryIo;
impl std::io::Read for MemoryIo {
    fn read(&mut self, _: &mut [u8]) -> std::io::Result<usize> {
        Err(std::io::ErrorKind::WouldBlock.into())
    }
}
impl std::io::Write for MemoryIo {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
#[test]
fn actual_tls_callbacks_refuse_foreign_retired_and_reused_contexts_without_dereference() {
    let _serial = serial();
    for mode in ["late", "reused", "foreign"] {
        let mut identity = Identity::generate(OsEntropy::open()).unwrap();
        let directory = tls_controls(&mut identity, &format!("tls-callback-{mode}"));
        let root = fixture_bytes(&directory, "root.der");
        let leaf = fixture_bytes(&directory, "server.der");
        let clear = fixture_bytes(&directory, "clear.crl");
        let state =
            RevocationState::from_trusted_floor(certificate_policy(&directory).issuer_sha256, 1)
                .unwrap();
        let bound = identity
            .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
            .unwrap();
        let mut session = bound
            .tls_server(
                MemoryIo,
                &root,
                tls_policy(&directory),
                TlsCancellation::default(),
            )
            .unwrap();
        assert!(session.qualification_valid_bio());
        let ticket = session.qualification_bio_ticket();
        if mode == "reused" {
            session.close();
            drop(session);
            let bound = identity
                .bind_current_certificate(
                    &leaf,
                    &root,
                    certificate_policy(&directory),
                    &state,
                    &clear,
                )
                .unwrap();
            session = bound
                .tls_server(
                    MemoryIo,
                    &root,
                    tls_policy(&directory),
                    TlsCancellation::default(),
                )
                .unwrap();
            assert!(session.qualification_valid_bio());
            assert!(session.qualification_late_bio(ticket, true));
        } else if mode == "late" {
            assert!(session.qualification_late_bio(ticket, false));
        } else {
            assert!(session.qualification_foreign_bio());
        }
        assert_eq!(
            session.poll_handshake(),
            Err(TlsError::Identity(Error::Poisoned))
        );
        drop(session);
        assert!(matches!(
            identity.request("cam_A", &mut [0; 2048]),
            Err(Error::Poisoned)
        ));
        drop(identity);
        assert!(Identity::generate(OsEntropy::open()).is_ok());
    }
}
struct CancelAfterRecord {
    socket: std::net::TcpStream,
    armed: Arc<AtomicBool>,
    triggered: Arc<AtomicBool>,
    cancellation: TlsCancellation,
}
impl std::io::Read for CancelAfterRecord {
    fn read(&mut self, output: &mut [u8]) -> std::io::Result<usize> {
        let n = self.socket.read(output)?;
        if n > 5 && self.armed.swap(false, Ordering::AcqRel) {
            self.triggered.store(true, Ordering::Release);
            self.cancellation.cancel();
        }
        Ok(n)
    }
}
impl std::io::Write for CancelAfterRecord {
    fn write(&mut self, input: &[u8]) -> std::io::Result<usize> {
        self.socket.write(input)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
#[test]
fn actual_tls_plaintext_is_cleared_on_post_read_cancellation_and_partial_record_deadline() {
    use std::io::Read;
    let _serial = serial();
    let mut identity = Identity::generate(OsEntropy::open()).unwrap();
    let directory = tls_controls(&mut identity, "tls-partial-read");
    let root = fixture_bytes(&directory, "root.der");
    let leaf = fixture_bytes(&directory, "server.der");
    let clear = fixture_bytes(&directory, "clear.crl");
    let state =
        RevocationState::from_trusted_floor(certificate_policy(&directory).issuer_sha256, 1)
            .unwrap();
    for fragmented in [false, true] {
        let bound = identity
            .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
            .unwrap();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let mut client = tls_client(
            &directory,
            listener.local_addr().unwrap().port(),
            "valid",
            "13",
        );
        let socket = accepted(&listener);
        socket.set_nonblocking(true).unwrap();
        let cancellation = TlsCancellation::default();
        let mut bytes = [0xa5; 256];
        if fragmented {
            let chunk = Arc::new(AtomicUsize::new(8192));
            let read_bytes = Arc::new(AtomicUsize::new(0));
            let stream = Fragmented {
                socket,
                hold: Arc::new(AtomicBool::new(false)),
                chunk: chunk.clone(),
                pending: Arc::new(AtomicBool::new(false)),
                read_bytes: read_bytes.clone(),
            };
            let mut policy = tls_policy(&directory);
            policy.record_timeout = std::time::Duration::from_millis(30);
            let mut session = bound
                .tls_server(stream, &root, policy, cancellation)
                .unwrap();
            handshake(&mut session).unwrap();
            chunk.store(1, Ordering::Release);
            read_bytes.store(0, Ordering::Release);
            let started = std::time::Instant::now();
            loop {
                assert_eq!(
                    session.read(&mut bytes).unwrap_err().kind(),
                    std::io::ErrorKind::WouldBlock
                );
                assert!(bytes.iter().all(|byte| *byte == 0));
                if read_bytes.load(Ordering::Acquire) > 0 {
                    break;
                }
                assert!(started.elapsed() < std::time::Duration::from_secs(3));
                std::thread::sleep(std::time::Duration::from_millis(1));
            }
            // The native per-operation callback budget left a real incomplete
            // record. Pause its next read across the whole-record deadline.
            assert!(read_bytes.load(Ordering::Acquire) <= 8);
            std::thread::sleep(std::time::Duration::from_millis(40));
            assert_eq!(
                session.read(&mut bytes).unwrap_err().kind(),
                std::io::ErrorKind::TimedOut
            );
            assert!(bytes.iter().all(|byte| *byte == 0));
            assert_eq!(session.poll_handshake(), Err(TlsError::Deadline));
        } else {
            let armed = Arc::new(AtomicBool::new(false));
            let triggered = Arc::new(AtomicBool::new(false));
            let stream = CancelAfterRecord {
                socket,
                armed: armed.clone(),
                triggered: triggered.clone(),
                cancellation: cancellation.clone(),
            };
            let mut session = bound
                .tls_server(stream, &root, tls_policy(&directory), cancellation)
                .unwrap();
            handshake(&mut session).unwrap();
            armed.store(true, Ordering::Release);
            let started = std::time::Instant::now();
            loop {
                let result = session.read(&mut bytes);
                assert!(bytes.iter().all(|byte| *byte == 0));
                if let Err(error) = result {
                    if error.kind() == std::io::ErrorKind::ConnectionAborted {
                        break;
                    }
                }
                assert!(started.elapsed() < std::time::Duration::from_secs(3));
                std::thread::sleep(std::time::Duration::from_millis(1));
            }
            assert!(triggered.load(Ordering::Acquire));
            assert_eq!(session.poll_handshake(), Err(TlsError::Cancelled));
        }
        wait_client(&mut client, false);
        assert!(!identity.failed());
    }
}

struct DropIo<'key, 'entropy> {
    owner: &'key Identity<'entropy>,
    closed: Arc<AtomicBool>,
}
impl std::io::Read for DropIo<'_, '_> {
    fn read(&mut self, _: &mut [u8]) -> std::io::Result<usize> {
        Err(std::io::ErrorKind::WouldBlock.into())
    }
}
impl std::io::Write for DropIo<'_, '_> {
    fn write(&mut self, input: &[u8]) -> std::io::Result<usize> {
        Ok(input.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
impl Drop for DropIo<'_, '_> {
    fn drop(&mut self) {
        assert!(
            self.owner.tls_owned.get(),
            "TLS lease released before owned I/O finished dropping"
        );
        self.closed.store(true, Ordering::Release);
    }
}
struct SlowRefresh {
    os: OsEntropy,
    slow: Arc<AtomicBool>,
}
impl Entropy for SlowRefresh {
    fn fill(&mut self, output: &mut [u8]) -> Result<(), EntropyUnavailable> {
        if self.slow.load(Ordering::Acquire) {
            std::thread::sleep(std::time::Duration::from_millis(70));
        }
        self.os.fill(output)
    }
}
#[test]
fn actual_tls_constructor_retires_io_before_lease_and_rejects_fresh_stale_time() {
    let _serial = serial();
    let slow = Arc::new(AtomicBool::new(false));
    let mut identity = Identity::generate(SlowRefresh {
        os: OsEntropy::open(),
        slow: slow.clone(),
    })
    .unwrap();
    let directory = tls_controls(&mut identity, "tls-constructor");
    let root = fixture_bytes(&directory, "root.der");
    let leaf = fixture_bytes(&directory, "server.der");
    let clear = fixture_bytes(&directory, "clear.crl");
    // Each failure mode starts with independent trust state. Within a mode,
    // trusted time must never be reset to reopen an expired observation.
    for mode in ["root", "stale", "slow", "near-expiry"] {
        let state =
            RevocationState::from_trusted_floor(certificate_policy(&directory).issuer_sha256, 1)
                .unwrap();
        let bound = identity
            .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
            .unwrap();
        let closed = Arc::new(AtomicBool::new(false));
        let transport = DropIo {
            owner: &identity,
            closed: closed.clone(),
        };
        let mut policy = tls_policy(&directory);
        let candidate = if mode == "root" {
            vec![0]
        } else {
            root.clone()
        };
        let calls = identity.entropy_calls();
        if mode == "stale" {
            policy.camera.trusted_unix_seconds += 86400;
        }
        if mode == "slow" {
            policy.handshake_timeout = std::time::Duration::from_millis(30);
            slow.store(true, Ordering::Release);
        }
        if mode == "near-expiry" {
            policy.camera.trusted_unix_seconds += 86399;
        }
        let result = bound.tls_server(transport, &candidate, policy, TlsCancellation::default());
        slow.store(false, Ordering::Release);
        if mode == "near-expiry" {
            let mut session = result.unwrap();
            assert!(session.remaining_validity().unwrap() <= std::time::Duration::from_secs(1));
            std::thread::sleep(
                session.remaining_validity().unwrap() + std::time::Duration::from_millis(20),
            );
            assert_eq!(
                session.poll_handshake(),
                Err(TlsError::Identity(Error::Expired))
            );
            drop(session);
        } else {
            assert!(matches!(
                result,
                Err(TlsError::Identity(Error::Certificate))
                    | Err(TlsError::Identity(Error::Expired))
                    | Err(TlsError::Deadline)
            ));
        }
        assert!(closed.load(Ordering::Acquire));
        assert!(!identity.tls_owned.get());
        assert!(!identity.failed());
        if mode == "stale" {
            assert_eq!(
                identity.entropy_calls(),
                calls,
                "stale trusted time must refuse before reseeding"
            );
        }
        if mode == "stale" || mode == "near-expiry" {
            assert!(
                matches!(
                    identity.bind_current_certificate(
                        &leaf,
                        &root,
                        certificate_policy(&directory),
                        &state,
                        &clear
                    ),
                    Err(Error::InvalidInput)
                ),
                "older trusted time reopened an expired cache"
            );
        }
    }
    // Normal close and busy admission preserve the same barrier.
    let state =
        RevocationState::from_trusted_floor(certificate_policy(&directory).issuer_sha256, 1)
            .unwrap();
    let bound = identity
        .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
        .unwrap();
    let closed = Arc::new(AtomicBool::new(false));
    let mut session = bound
        .tls_server(
            DropIo {
                owner: &identity,
                closed: closed.clone(),
            },
            &root,
            tls_policy(&directory),
            TlsCancellation::default(),
        )
        .unwrap();
    let extra = identity
        .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
        .unwrap();
    assert!(matches!(
        extra.tls_server(
            MemoryIo,
            &root,
            tls_policy(&directory),
            TlsCancellation::default()
        ),
        Err(TlsError::Busy)
    ));
    session.close();
    assert!(closed.load(Ordering::Acquire));
    assert!(!identity.tls_owned.get());
}

#[test]
fn actual_current_binding_expires_after_a_fresher_trusted_clock_observation() {
    let _serial = serial();
    let mut identity = Identity::generate(OsEntropy::open()).unwrap();
    let directory = tls_controls(&mut identity, "tls-clock-expiry");
    let root = fixture_bytes(&directory, "root.der");
    let leaf = fixture_bytes(&directory, "short-server.der");
    let clear = fixture_bytes(&directory, "clear.crl");
    let state =
        RevocationState::from_trusted_floor(certificate_policy(&directory).issuer_sha256, 1)
            .unwrap();
    let binding = identity
        .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
        .unwrap();
    assert!(binding.certificate_der().is_ok());
    let now = certificate_policy(&directory).trusted_unix_seconds;
    // A still-valid issuer CRL at this later trusted UTC must expire the old
    // certificate immediately, without waiting for its original local lease.
    state.observe(&identity, &root, &clear, now + 61).unwrap();
    assert!(matches!(binding.certificate_der(), Err(Error::Expired)));
    assert!(matches!(
        identity.bind_current_certificate(
            &leaf,
            &root,
            certificate_policy(&directory),
            &state,
            &clear
        ),
        Err(Error::InvalidInput)
    ));
}

#[test]
fn actual_tls_stream_reuses_http_framing_and_reconnects_without_cleartext() {
    use std::io::{Read, Write};
    let _serial = serial();
    let mut identity = Identity::generate(OsEntropy::open()).unwrap();
    let directory = tls_controls(&mut identity, "tls-mjpeg");
    let root = fixture_bytes(&directory, "root.der");
    let leaf = fixture_bytes(&directory, "server.der");
    let clear = fixture_bytes(&directory, "clear.crl");
    let image = include_bytes!("frame.jpg");
    let state =
        RevocationState::from_trusted_floor(certificate_policy(&directory).issuer_sha256, 1)
            .unwrap();
    let attempts = Cell::new(0);
    let mut server = stream_service::Server::with_transport(
        std::net::TcpListener::bind("127.0.0.1:0").unwrap(),
        vec![0; 153600],
        stream_service::Options {
            request_timeout_ms: 10000,
            ..stream_service::Options::default()
        },
        |socket| {
            attempts.set(attempts.get() + 1);
            identity
                .bind_current_certificate(
                    &leaf,
                    &root,
                    certificate_policy(&directory),
                    &state,
                    &clear,
                )
                .map_err(|error| std::io::Error::from(TlsError::from(error)))?
                .tls_server_tcp(
                    socket,
                    &root,
                    tls_policy(&directory),
                    TlsCancellation::default(),
                )
                .map_err(std::io::Error::from)
        },
    )
    .unwrap();
    let started = std::time::Instant::now();
    // A real cleartext request must close without an HTTP response or JPEG.
    let mut cleartext = std::net::TcpStream::connect(server.local_addr().unwrap()).unwrap();
    cleartext
        .write_all(b"GET /stream HTTP/1.1\r\nHost: localhost\r\n\r\n")
        .unwrap();
    cleartext.set_nonblocking(true).unwrap();
    while server.stats().disconnected < 1 {
        server
            .poll(started.elapsed().as_millis() as u64, None)
            .unwrap();
        assert!(started.elapsed() < std::time::Duration::from_secs(5));
        std::thread::sleep(std::time::Duration::from_millis(1));
    }
    let mut response = [0; 1024];
    if let Ok(n) = cleartext.read(&mut response) {
        assert!(!response[..n].starts_with(b"HTTP/"));
        assert!(!response[..n].windows(2).any(|bytes| bytes == [0xff, 0xd8]));
    }
    drop(cleartext);
    for version in ["12", "13"] {
        let before = server.stats();
        let mut client = tls_client(
            &directory,
            server.local_addr().unwrap().port(),
            "mjpeg",
            version,
        );
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(12);
        let mut sequence = 0;
        let mut client_finished = false;
        loop {
            let now = started.elapsed().as_millis() as u64;
            sequence += 1;
            let latest = camera::Frame {
                sequence,
                capture: camera::Capture {
                    format: camera::Format {
                        width: 320,
                        height: 240,
                        pixels: camera::PixelFormat::Jpeg,
                    },
                    len: image.len(),
                    timestamp_ms: now,
                },
                bytes: image,
            };
            let progress = server.poll(now, Some(latest)).unwrap();
            let stats = server.stats();
            assert!(stats.accepted - stats.disconnected <= 1);
            if !client_finished {
                if let Some(status) = client.try_wait().unwrap() {
                    assert!(status.success(), "actual TLS multipart client failed");
                    client_finished = true;
                }
            }
            if client_finished && !progress.active {
                break;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "TLS multipart/reconnect deadline"
            );
            std::thread::sleep(std::time::Duration::from_millis(1));
        }
        assert_eq!(server.stats().accepted, before.accepted + 1);
        assert_eq!(server.stats().disconnected, before.disconnected + 1);
        assert!(server.stats().frames_completed >= before.frames_completed + 2);
        assert!(!identity.tls_owned.get());
        assert!(!identity.failed());
    }
    assert_eq!(attempts.get(), 3);
}

#[test]
fn actual_tls_refuses_legacy_protocol_ciphers_and_malformed_wire_then_recovers() {
    use std::io::Write;
    let _serial = serial();
    let mut identity = Identity::generate(OsEntropy::open()).unwrap();
    let directory = tls_controls(&mut identity, "tls-wire");
    let root = fixture_bytes(&directory, "root.der");
    let leaf = fixture_bytes(&directory, "server.der");
    let clear = fixture_bytes(&directory, "clear.crl");
    let state =
        RevocationState::from_trusted_floor(certificate_policy(&directory).issuer_sha256, 1)
            .unwrap();
    for (label, version) in [("valid", "11"), ("unsupported-suite", "12")] {
        let binding = identity
            .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
            .unwrap();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let mut client = tls_client(
            &directory,
            listener.local_addr().unwrap().port(),
            label,
            version,
        );
        let socket = accepted(&listener);
        let mut session = binding
            .tls_server_tcp(
                socket,
                &root,
                tls_policy(&directory),
                TlsCancellation::default(),
            )
            .unwrap();
        assert_eq!(handshake(&mut session), Err(TlsError::Protocol));
        wait_client(&mut client, false);
        // A locally unsupported client cipher/protocol cannot satisfy this
        // control. Its private SSL error must report the actual peer alert.
        let diagnostic = std::fs::read_to_string(
            directory.join(format!("client-{label}-{version}-private.err")),
        )
        .unwrap();
        assert!(diagnostic.to_ascii_lowercase().contains("alert"));
        drop(session);
        assert!(!identity.tls_owned.get());
        assert!(!identity.failed());
    }
    let mut oversized = vec![0x16, 0x03, 0x03, 0x10, 0x01];
    oversized.extend_from_slice(&[0; 4097]);
    for record in [
        vec![0x17, 0x03, 0x03, 0, 1, 0],
        vec![0x16, 0x03, 0x03, 0, 4, 0xff, 0, 0, 0],
        vec![0x16, 0x03, 0x03, 0, 4, 1, 0, 0, 0],
        oversized,
    ] {
        let binding = identity
            .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
            .unwrap();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let mut peer = std::net::TcpStream::connect(listener.local_addr().unwrap()).unwrap();
        peer.set_write_timeout(Some(std::time::Duration::from_secs(1)))
            .unwrap();
        peer.write_all(&record).unwrap();
        let mut session = binding
            .tls_server_tcp(
                accepted(&listener),
                &root,
                tls_policy(&directory),
                TlsCancellation::default(),
            )
            .unwrap();
        assert_eq!(handshake(&mut session), Err(TlsError::Protocol));
        assert!(matches!(
            session.negotiated_version(),
            Err(TlsError::Protocol)
        ));
        drop(session);
        assert!(!identity.tls_owned.get());
        assert!(!identity.failed());
    }
    let binding = identity
        .bind_current_certificate(&leaf, &root, certificate_policy(&directory), &state, &clear)
        .unwrap();
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let mut client = tls_client(
        &directory,
        listener.local_addr().unwrap().port(),
        "valid",
        "13",
    );
    let mut session = binding
        .tls_server_tcp(
            accepted(&listener),
            &root,
            tls_policy(&directory),
            TlsCancellation::default(),
        )
        .unwrap();
    handshake(&mut session).unwrap();
    session.close();
    wait_client(&mut client, false); // Healthy admission succeeded, then owner closed before the response.
}

struct HeldRefreshEntropy {
    os: OsEntropy,
    armed: Arc<AtomicBool>,
    release: Arc<AtomicBool>,
    entered: std::sync::mpsc::SyncSender<()>,
    retired: Arc<AtomicBool>,
}
impl Entropy for HeldRefreshEntropy {
    fn fill(&mut self, output: &mut [u8]) -> Result<(), EntropyUnavailable> {
        if self.armed.swap(false, Ordering::AcqRel) {
            self.entered.try_send(()).map_err(|_| EntropyUnavailable)?;
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(3);
            while !self.release.load(Ordering::Acquire) {
                if std::time::Instant::now() >= deadline {
                    return Err(EntropyUnavailable);
                }
                std::thread::sleep(std::time::Duration::from_millis(1));
            }
        }
        self.os.fill(output)
    }
}
impl Drop for HeldRefreshEntropy {
    fn drop(&mut self) {
        assert!(
            OWNED.load(Ordering::Acquire),
            "key lease released before worker source retirement"
        );
        self.retired.store(true, Ordering::Release);
    }
}
struct SyntheticWorkerCamera {
    format: Option<camera::Format>,
    refuse_stop_once: bool,
}
impl camera::Camera for SyntheticWorkerCamera {
    fn start(&mut self, format: camera::Format) -> Result<camera::Format, camera::DeviceError> {
        self.format = Some(format);
        Ok(format)
    }
    fn poll_frame(
        &mut self,
        now: u64,
        out: &mut [u8],
    ) -> Result<Option<camera::Capture>, camera::DeviceError> {
        let image = include_bytes!("frame.jpg");
        out[..image.len()].copy_from_slice(image);
        Ok(Some(camera::Capture {
            format: self.format.ok_or(camera::DeviceError::Io)?,
            len: image.len(),
            timestamp_ms: now,
        }))
    }
    fn stop(&mut self) -> Result<(), camera::DeviceError> {
        if self.refuse_stop_once {
            self.refuse_stop_once = false;
            return Err(camera::DeviceError::Busy);
        }
        self.format = None;
        Ok(())
    }
}
#[test]
fn actual_tls_worker_preserves_capture_and_native_retirement_during_held_entropy() {
    use stream_service::{FramePublish, StreamWorker, WorkerOptions};
    let _serial = serial();
    let (ready_send, ready) = std::sync::mpsc::sync_channel(1);
    let (entered_send, entered) = std::sync::mpsc::sync_channel(1);
    let armed = Arc::new(AtomicBool::new(false));
    let worker_armed = armed.clone();
    let release = Arc::new(AtomicBool::new(false));
    let worker_release = release.clone();
    let retired = Arc::new(AtomicBool::new(false));
    let worker_retired = retired.clone();
    let epoch = std::time::Instant::now();
    let worker = StreamWorker::spawn(
        vec![0; 153600],
        WorkerOptions {
            stack_bytes: 1024 * 1024,
            poll_interval: std::time::Duration::from_millis(2),
        },
        move |mut frames, stop| {
            // The !Send SDK owner, live signing key and trust state are created on
            // this network worker. Only public readiness crosses back to capture.
            let mut identity = Identity::generate(HeldRefreshEntropy {
                os: OsEntropy::open(),
                armed: worker_armed,
                release: worker_release,
                entered: entered_send,
                retired: worker_retired,
            })
            .map_err(|e| std::io::Error::from(TlsError::from(e)))?;
            let directory = tls_controls(&mut identity, "tls-owner-worker");
            let root = fixture_bytes(&directory, "root.der");
            let leaf = fixture_bytes(&directory, "server.der");
            let clear = fixture_bytes(&directory, "clear.crl");
            let state = RevocationState::from_trusted_floor(
                certificate_policy(&directory).issuer_sha256,
                1,
            )
            .unwrap();
            let listener = std::net::TcpListener::bind("127.0.0.1:0")?;
            let port = listener.local_addr()?.port();
            let mut server = stream_service::Server::with_transport(
                listener,
                vec![0; 153600],
                stream_service::Options {
                    request_timeout_ms: 10000,
                    ..stream_service::Options::default()
                },
                |socket| {
                    identity
                        .bind_current_certificate(
                            &leaf,
                            &root,
                            certificate_policy(&directory),
                            &state,
                            &clear,
                        )
                        .map_err(|e| std::io::Error::from(TlsError::from(e)))?
                        .tls_server_tcp(
                            socket,
                            &root,
                            tls_policy(&directory),
                            TlsCancellation::default(),
                        )
                        .map_err(std::io::Error::from)
                },
            )?;
            ready_send.send((directory.clone(), port)).unwrap();
            let result = frames.serve(&mut server, &stop, || epoch.elapsed().as_millis() as u64);
            drop(server);
            assert!(!identity.tls_owned.get());
            drop(identity);
            result
        },
    )
    .unwrap();
    let (directory, port) = ready
        .recv_timeout(std::time::Duration::from_secs(10))
        .unwrap();
    let mut camera = camera_stream::ThreadedCamera::<_, 153600, _, _>::new(
        SyntheticWorkerCamera {
            format: None,
            refuse_stop_once: true,
        },
        vec![0; 153600],
        vec![0; 153600],
        worker,
    )
    .unwrap();
    camera.start(320, 240).unwrap();
    camera
        .poll(epoch.elapsed().as_millis() as u64, true)
        .unwrap();
    armed.store(true, Ordering::Release);
    let mut client = tls_client(&directory, port, "mjpeg", "13");
    entered
        .recv_timeout(std::time::Duration::from_secs(3))
        .unwrap();
    let baseline = camera.stats();
    let started = std::time::Instant::now();
    for _ in 0..1000 {
        assert_eq!(
            camera
                .poll(epoch.elapsed().as_millis() as u64, true)
                .unwrap(),
            Some(FramePublish::Busy)
        );
    }
    assert!(started.elapsed() < std::time::Duration::from_millis(300));
    assert_eq!(
        camera.stats().capture.captured,
        baseline.capture.captured + 1000
    );
    assert_eq!(camera.stats().skipped_busy, baseline.skipped_busy + 1000);
    assert!(!retired.load(Ordering::Acquire));
    assert!(matches!(
        Identity::generate(OsEntropy::open()),
        Err(Error::Busy)
    ));
    release.store(true, Ordering::Release);
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
    let mut client_done = false;
    loop {
        camera
            .poll(epoch.elapsed().as_millis() as u64, true)
            .unwrap();
        if !client_done {
            if let Some(status) = client.try_wait().unwrap() {
                assert!(status.success(), "worker SDK multipart client failed");
                client_done = true;
            }
        }
        let stats = camera.stats().capture.stream;
        if client_done && stats.accepted == 1 && stats.disconnected == 1 {
            break;
        }
        assert!(std::time::Instant::now() < deadline);
        std::thread::sleep(std::time::Duration::from_millis(1));
    }
    assert!(camera.stats().capture.stream.frames_completed >= 2);
    // Start another genuine SDK refresh and stop while that callback is held.
    release.store(false, Ordering::Release);
    armed.store(true, Ordering::Release);
    let mut interrupted = tls_client(&directory, port, "mjpeg", "13");
    entered
        .recv_timeout(std::time::Duration::from_secs(3))
        .unwrap();
    let started = std::time::Instant::now();
    assert!(
        camera.stop().is_err(),
        "camera stop refusal must retain its retry path"
    );
    camera.stop().unwrap();
    assert!(started.elapsed() < std::time::Duration::from_millis(300));
    let (finished_send, finished) = std::sync::mpsc::sync_channel(1);
    let joiner =
        std::thread::spawn(move || finished_send.send(camera.finish_worker().is_ok()).unwrap());
    assert!(finished
        .recv_timeout(std::time::Duration::from_millis(30))
        .is_err());
    assert!(!retired.load(Ordering::Acquire));
    assert!(matches!(
        Identity::generate(OsEntropy::open()),
        Err(Error::Busy)
    ));
    release.store(true, Ordering::Release);
    assert!(finished
        .recv_timeout(std::time::Duration::from_secs(3))
        .unwrap());
    joiner.join().unwrap();
    wait_client(&mut interrupted, false);
    assert!(retired.load(Ordering::Acquire));
    assert!(Identity::generate(OsEntropy::open()).is_ok());
}

#[test]
fn actual_tls_worker_applies_live_trust_updates_outside_capture_lock() {
    use std::sync::mpsc;
    use std::time::{Duration, Instant};
    use stream_service::{FramePublish, StreamWorker, WorkerAction, WorkerOptions};
    let _serial = serial();
    #[derive(Clone, Copy)]
    enum Control {
        Refresh,
        Malformed,
        Revoke,
        Fatal,
    }
    let (commands_send, commands) = mpsc::sync_channel(1);
    let (replies_send, replies) = mpsc::sync_channel(1);
    let (ready_send, ready) = mpsc::sync_channel(1);
    let (entered_send, entered) = mpsc::sync_channel(1);
    let armed = Arc::new(AtomicBool::new(false));
    let worker_armed = armed.clone();
    let release = Arc::new(AtomicBool::new(false));
    let worker_release = release.clone();
    let retired = Arc::new(AtomicBool::new(false));
    let worker_retired = retired.clone();
    let epoch = Instant::now();
    let worker = StreamWorker::spawn(
        vec![0; 153600],
        WorkerOptions {
            stack_bytes: 1024 * 1024,
            poll_interval: Duration::from_millis(2),
        },
        move |mut frames, stop| {
            let mut identity = Identity::generate(HeldRefreshEntropy {
                os: OsEntropy::open(),
                armed: worker_armed,
                release: worker_release,
                entered: entered_send,
                retired: worker_retired,
            })
            .map_err(|e| std::io::Error::from(TlsError::from(e)))?;
            let directory = tls_controls(&mut identity, "tls-worker-live-control");
            let root = fixture_bytes(&directory, "root.der");
            let leaf = fixture_bytes(&directory, "server.der");
            let clear = fixture_bytes(&directory, "clear.crl");
            let revoked = fixture_bytes(&directory, "revoked.crl");
            let current = Cell::new(clear.as_slice());
            let state = RevocationState::from_trusted_floor(
                certificate_policy(&directory).issuer_sha256,
                1,
            )
            .unwrap();
            let listener = std::net::TcpListener::bind("127.0.0.1:0")?;
            let port = listener.local_addr()?.port();
            let mut server = stream_service::Server::with_transport(
                listener,
                vec![0; 153600],
                stream_service::Options {
                    request_timeout_ms: 10000,
                    ..stream_service::Options::default()
                },
                |socket| {
                    identity
                        .bind_current_certificate(
                            &leaf,
                            &root,
                            certificate_policy(&directory),
                            &state,
                            current.get(),
                        )
                        .map_err(|e| std::io::Error::from(TlsError::from(e)))?
                        .tls_server_tcp(
                            socket,
                            &root,
                            tls_policy(&directory),
                            TlsCancellation::default(),
                        )
                        .map_err(std::io::Error::from)
                },
            )?;
            ready_send.send((directory.clone(), port)).unwrap();
            let result = frames.serve_with_control(
                &mut server,
                &stop,
                || epoch.elapsed().as_millis() as u64,
                || {
                    let control = match commands.try_recv() {
                        Ok(value) => value,
                        Err(_) => return Ok(WorkerAction::Continue),
                    };
                    let now = certificate_policy(&directory).trusted_unix_seconds;
                    let disconnect = matches!(control, Control::Revoke);
                    let result = match control {
                        Control::Refresh => identity.refresh_entropy().map(|()| 0),
                        Control::Malformed => state.observe(&identity, &root, b"invalid CRL", now),
                        Control::Revoke => {
                            let result = state.observe(&identity, &root, &revoked, now);
                            if result.is_ok() {
                                current.set(revoked.as_slice());
                            }
                            result
                        }
                        Control::Fatal => return Err(std::io::ErrorKind::PermissionDenied.into()),
                    };
                    let action = if disconnect && result.is_ok() {
                        WorkerAction::Disconnect
                    } else {
                        WorkerAction::Continue
                    };
                    replies_send.try_send(result).unwrap();
                    Ok(action)
                },
            );
            drop(server);
            assert!(!identity.tls_owned.get());
            drop(identity);
            result
        },
    )
    .unwrap();
    let (directory, port) = ready.recv_timeout(Duration::from_secs(10)).unwrap();
    let mut camera = camera_stream::ThreadedCamera::<_, 153600, _, _>::new(
        SyntheticWorkerCamera {
            format: None,
            refuse_stop_once: false,
        },
        vec![0; 153600],
        vec![0; 153600],
        worker,
    )
    .unwrap();
    camera.start(320, 240).unwrap();
    camera
        .poll(epoch.elapsed().as_millis() as u64, false)
        .unwrap();
    armed.store(true, Ordering::Release);
    commands_send.send(Control::Refresh).unwrap();
    entered.recv_timeout(Duration::from_secs(3)).unwrap();
    let baseline = camera.stats().capture.captured;
    let started = Instant::now();
    for _ in 0..1000 {
        assert_eq!(
            camera
                .poll(epoch.elapsed().as_millis() as u64, false)
                .unwrap(),
            Some(FramePublish::Published)
        );
    }
    assert!(started.elapsed() < Duration::from_millis(300));
    assert_eq!(camera.stats().capture.captured, baseline + 1000);
    assert!(!retired.load(Ordering::Acquire));
    assert!(matches!(
        Identity::generate(OsEntropy::open()),
        Err(Error::Busy)
    ));
    release.store(true, Ordering::Release);
    assert_eq!(replies.recv_timeout(Duration::from_secs(3)).unwrap(), Ok(0));
    let mut active = tls_client(&directory, port, "mjpeg-live", "13");
    let deadline = Instant::now() + Duration::from_secs(10);
    while !directory.join("worker-mjpeg-ready").is_file() {
        camera
            .poll(epoch.elapsed().as_millis() as u64, true)
            .unwrap();
        assert!(
            active.try_wait().unwrap().is_none(),
            "live TLS client exited before two validated JPEGs"
        );
        assert!(Instant::now() < deadline, "live TLS media readiness");
        std::thread::sleep(Duration::from_millis(1));
    }
    commands_send.send(Control::Malformed).unwrap();
    assert!(replies
        .recv_timeout(Duration::from_secs(3))
        .unwrap()
        .is_err());
    let before = camera.stats().capture.stream.frames_completed;
    let deadline = Instant::now() + Duration::from_secs(3);
    while camera.stats().capture.stream.frames_completed < before + 2 {
        camera
            .poll(epoch.elapsed().as_millis() as u64, true)
            .unwrap();
        assert!(
            active.try_wait().unwrap().is_none(),
            "malformed update retired healthy stream"
        );
        assert!(
            Instant::now() < deadline,
            "healthy stream progress after malformed update"
        );
        std::thread::sleep(Duration::from_millis(1));
    }
    commands_send.send(Control::Revoke).unwrap();
    assert!(
        replies
            .recv_timeout(Duration::from_secs(3))
            .unwrap()
            .unwrap()
            > 0
    );
    wait_client(&mut active, true); // Two real JPEGs followed by bounded terminal closure.
    camera
        .poll(epoch.elapsed().as_millis() as u64, true)
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(3);
    while camera.stats().capture.stream.disconnected < 1 {
        camera
            .poll(epoch.elapsed().as_millis() as u64, true)
            .unwrap();
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(1));
    }
    let completed = camera.stats().capture.stream.frames_completed;
    let mut refused = tls_client(&directory, port, "mjpeg", "13");
    wait_client(&mut refused, false);
    for _ in 0..100 {
        camera
            .poll(epoch.elapsed().as_millis() as u64, true)
            .unwrap();
        std::thread::sleep(Duration::from_millis(1));
    }
    let stats = camera.stats().capture.stream;
    assert_eq!((stats.accepted, stats.disconnected), (2, 2));
    assert_eq!(
        stats.frames_completed, completed,
        "revoked peer received additional media"
    );
    commands_send.send(Control::Fatal).unwrap();
    let deadline = Instant::now() + Duration::from_secs(3);
    while !retired.load(Ordering::Acquire) {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(1));
    }
    camera.stop().unwrap();
    assert!(
        matches!(camera.finish_worker(),Err(camera_stream::Error::Network(error)) if error.kind()==std::io::ErrorKind::PermissionDenied)
    );
    assert!(retired.load(Ordering::Acquire));
    assert!(Identity::generate(OsEntropy::open()).is_ok());
}
