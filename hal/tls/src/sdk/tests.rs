//! Real SDK controls; no entropy, private keys or CSR contents enter logs.
use super::*;
use std::{
    alloc::{GlobalAlloc, System},
    sync::{atomic::AtomicUsize, Arc, Mutex},
};

static SERIAL: Mutex<()> = Mutex::new(());
pub(super) fn serial() -> std::sync::MutexGuard<'static, ()> {
    SERIAL.lock().unwrap_or_else(|error| error.into_inner())
}
struct Source {
    os: crate::platform::Source,
    fail: Arc<AtomicBool>,
    calls: Arc<AtomicUsize>,
    closed: Arc<AtomicBool>,
}
impl Source {
    fn new() -> Self {
        Self {
            os: crate::platform::Source,
            fail: Arc::new(AtomicBool::new(false)),
            calls: Arc::new(AtomicUsize::new(0)),
            closed: Arc::new(AtomicBool::new(false)),
        }
    }
}
impl Entropy for Source {
    fn fill(&mut self, out: &mut [u8]) -> Result<(), EntropyUnavailable> {
        self.calls.fetch_add(1, Ordering::Relaxed);
        if self.fail.load(Ordering::Acquire) {
            out.fill(0x5a);
            return Err(EntropyUnavailable);
        }
        self.os.fill(out)
    }
}
impl Drop for Source {
    fn drop(&mut self) {
        self.closed.store(true, Ordering::Release);
    }
}

// Deny only the calling thread's Rust allocations. Default alloc_zeroed/realloc
// use this same alloc hook; deallocation always preserves System's contract.
thread_local! { static BUDGET: Cell<Option<usize>> = const { Cell::new(None) }; }
struct Allocator;
unsafe impl GlobalAlloc for Allocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let denied = BUDGET
            .try_with(|budget| match budget.get() {
                Some(0) => true,
                Some(n) => {
                    budget.set(Some(n - 1));
                    false
                }
                None => false,
            })
            .unwrap_or(false);
        if denied {
            ptr::null_mut()
        } else {
            unsafe { System.alloc(layout) }
        }
    }
    unsafe fn dealloc(&self, pointer: *mut u8, layout: Layout) {
        unsafe { System.dealloc(pointer, layout) }
    }
}
#[global_allocator]
static ALLOCATOR: Allocator = Allocator;
fn budget<T>(count: usize, operation: impl FnOnce() -> T) -> T {
    struct Reset;
    impl Drop for Reset {
        fn drop(&mut self) {
            BUDGET.with(|value| value.set(None));
        }
    }
    BUDGET.with(|value| {
        assert!(value.get().is_none());
        value.set(Some(count));
    });
    let reset = Reset;
    let result = operation();
    drop(reset);
    result
}

#[test]
fn real_csr_exclusive_owner_moves_and_fresh_keys() {
    let _serial = serial();
    let source = Source::new();
    let calls = source.calls.clone();
    let identity = Identity::generate(source).unwrap();
    assert!(calls.load(Ordering::Relaxed) > 0);
    assert!(matches!(
        Identity::generate(Source::new()),
        Err(Error::Busy)
    ));
    // The callback scope borrows the current location; it cannot retain the old
    // owner address across safe moves. The SDK key context itself stays stable.
    let moved = Box::new(identity);
    let mut first = [0; 2048];
    let n = moved.request("device_A", &mut first).unwrap();
    assert!(first[..n].starts_with(b"-----BEGIN CERTIFICATE REQUEST-----\n"));
    assert!(first[n..].iter().all(|v| *v == 0));
    let identity = *moved;
    assert!(identity.request("device_A", &mut [0; 2048]).is_ok());
    drop(identity);
    let mut second = [0; 2048];
    let fresh = crate::open().unwrap();
    let m = fresh.request("device_A", &mut second).unwrap();
    assert!(first[..n] != second[..m]);
    if let Some(directory) = std::env::var_os("NXRS_TLS_PUBLIC_DIR") {
        use std::{io::Write, os::unix::fs::OpenOptionsExt};
        for (name, bytes) in [("first.csr", &first[..n]), ("second.csr", &second[..m])] {
            let mut file = std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(std::path::Path::new(&directory).join(name))
                .unwrap();
            file.write_all(bytes).unwrap();
        }
    }
}

#[test]
fn initial_entropy_failure_retires_and_fresh_owner_recovers() {
    let _serial = serial();
    let source = Source::new();
    let calls = source.calls.clone();
    let closed = source.closed.clone();
    source.fail.store(true, Ordering::Release);
    assert!(matches!(Identity::generate(source), Err(Error::Entropy)));
    assert!(calls.load(Ordering::Relaxed) > 0 && closed.load(Ordering::Acquire));
    assert!(Identity::generate(Source::new())
        .unwrap()
        .request("recovered", &mut [0; 2048])
        .is_ok());
}

#[test]
fn failed_refresh_cannot_use_cached_drbg_or_restore_this_owner() {
    let _serial = serial();
    let source = Source::new();
    let fail = source.fail.clone();
    let identity = Identity::generate(source).unwrap();
    assert!(!identity.is_poisoned());
    fail.store(true, Ordering::Release);
    assert_eq!(identity.refresh_entropy(), Err(Error::Entropy));
    assert!(identity.is_poisoned());
    fail.store(false, Ordering::Release);
    let mut output = [0xa5; 2048];
    assert_eq!(
        identity.request("device", &mut output),
        Err(Error::Poisoned)
    );
    assert!(output.iter().all(|v| *v == 0));
}

#[cfg(feature = "pairing")]
#[test]
fn poisoned_owner_retires_every_borrower_before_fresh_credentials_recover() {
    use std::io::{self, Read, Write};

    struct Transport(Arc<AtomicUsize>);
    impl Read for Transport {
        fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
            Err(io::ErrorKind::WouldBlock.into())
        }
    }
    impl Write for Transport {
        fn write(&mut self, _: &[u8]) -> io::Result<usize> {
            Err(io::ErrorKind::WouldBlock.into())
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }
    impl crate::Stream for Transport {
        fn prepare(&mut self) -> io::Result<()> {
            Ok(())
        }
        fn abort(&mut self) {
            self.0.fetch_add(1, Ordering::AcqRel);
        }
    }

    let _serial = serial();
    let source = Source::new();
    let fail = source.fail.clone();
    let closed = source.closed.clone();
    let identity = Identity::generate(source).unwrap();
    let key = identity.pairing_key().unwrap();
    let retired = Arc::new(AtomicUsize::new(0));
    let mut radio = identity
        .accept_pairing(Transport(retired.clone()), &key)
        .unwrap();
    let mut other = identity
        .accept_pairing(Transport(retired.clone()), &key)
        .unwrap();
    fail.store(true, Ordering::Release);
    assert_eq!(identity.refresh_entropy(), Err(Error::Entropy));
    fail.store(false, Ordering::Release);
    assert!(identity.is_poisoned());
    assert_eq!(radio.poll(), Err(Error::Poisoned));
    assert_eq!(other.poll(), Err(Error::Poisoned));
    assert!(matches!(
        Identity::generate(Source::new()),
        Err(Error::Busy)
    ));
    drop((radio, other));
    assert_eq!(retired.load(Ordering::Acquire), 2);
    drop(identity);
    assert!(closed.load(Ordering::Acquire));
    let fresh = Identity::generate(Source::new()).unwrap();
    assert!(!fresh.is_poisoned());
    assert_ne!(key, fresh.pairing_key().unwrap());
}

#[test]
fn automatic_drbg_reseed_failure_clears_csr_and_stays_closed() {
    let _serial = serial();
    let source = Source::new();
    let fail = source.fail.clone();
    let calls = source.calls.clone();
    let identity = Identity::generate(source).unwrap();
    let seeded = calls.load(Ordering::Relaxed);
    fail.store(true, Ordering::Release);
    let mut output = [0xa5; 2048];
    let mut refused = false;
    for _ in 0..2048 {
        if let Err(error) = identity.request("device", &mut output) {
            assert_eq!(error, Error::Entropy);
            refused = true;
            break;
        }
    }
    assert!(refused && calls.load(Ordering::Relaxed) > seeded);
    assert!(output.iter().all(|v| *v == 0));
    fail.store(false, Ordering::Release);
    assert_eq!(
        identity.request("device", &mut output),
        Err(Error::Poisoned)
    );
}

#[test]
fn invalid_subject_and_capacity_refuse_before_sdk_without_poisoning() {
    let _serial = serial();
    let source = Source::new();
    let calls = source.calls.clone();
    let identity = Identity::generate(source).unwrap();
    let before = calls.load(Ordering::Relaxed);
    let mut output = [0xa5; 2048];
    for subject in ["", "a/b", "a,CN=b", "a\0b", &"a".repeat(65)] {
        assert_eq!(
            identity.request(subject, &mut output),
            Err(Error::InvalidInput)
        );
        assert!(output.iter().all(|v| *v == 0));
    }
    for length in [0, 511, 4097] {
        assert_eq!(
            identity.request("device", &mut std::vec![0; length]),
            Err(Error::Capacity)
        );
    }
    assert_eq!(calls.load(Ordering::Relaxed), before);
    assert!(identity.request("device", &mut output).is_ok());
}

#[test]
fn foreign_and_late_callbacks_refuse_without_dereferencing_owner() {
    let _serial = serial();
    let identity = Identity::generate(Source::new()).unwrap();
    let probe = || {
        let mut bits = 99;
        let mut output = [0xa5; 32];
        assert_ne!(
            unsafe {
                mbedtls_platform_get_entropy(0, &mut bits, output.as_mut_ptr(), output.len())
            },
            0
        );
        assert_eq!(bits, 0);
        assert!(output.iter().all(|v| *v == 0));
    };
    std::thread::spawn(probe).join().unwrap();
    assert_eq!(
        identity.request("device", &mut [0; 2048]),
        Err(Error::Poisoned)
    );
    drop(identity);
    probe();
    assert!(Identity::generate(Source::new()).is_ok());
}

#[test]
fn unsupported_callback_flags_latch_failure() {
    let _serial = serial();
    let identity = Identity::generate(Source::new()).unwrap();
    let mut bits = 99;
    let mut output = [0xa5; 32];
    assert_eq!(
        identity.sdk(|| unsafe {
            mbedtls_platform_get_entropy(1, &mut bits, output.as_mut_ptr(), output.len())
        }),
        Err(Error::Entropy)
    );
    assert_eq!(bits, 0);
    assert!(output.iter().all(|v| *v == 0));
    assert_eq!(
        identity.request("device", &mut [0; 2048]),
        Err(Error::Poisoned)
    );
}

#[test]
fn reentrant_entropy_borrow_cannot_credit_a_failed_sample() {
    let _serial = serial();
    struct Reentrant;
    impl Entropy for Reentrant {
        fn fill(&mut self, output: &mut [u8]) -> Result<(), EntropyUnavailable> {
            let mut inner = [0xa5; 16];
            let mut bits = 99;
            assert_ne!(
                unsafe {
                    mbedtls_platform_get_entropy(0, &mut bits, inner.as_mut_ptr(), inner.len())
                },
                0
            );
            assert_eq!(bits, 0);
            assert!(inner.iter().all(|v| *v == 0));
            output.fill(0x5a);
            Ok(()) // Even a provider ignoring nested failure cannot credit it.
        }
    }
    assert!(matches!(Identity::generate(Reentrant), Err(Error::Entropy)));
    assert!(Identity::generate(Source::new()).is_ok());
}

#[test]
fn source_destruction_finishes_before_sdk_lease_is_released() {
    let _serial = serial();
    struct Observe(Source);
    impl Entropy for Observe {
        fn fill(&mut self, output: &mut [u8]) -> Result<(), EntropyUnavailable> {
            self.0.fill(output)
        }
    }
    impl Drop for Observe {
        fn drop(&mut self) {
            assert!(matches!(
                Identity::generate(Source::new()),
                Err(Error::Busy)
            ));
        }
    }
    drop(Identity::generate(Observe(Source::new())).unwrap());
    assert!(Identity::generate(Source::new()).is_ok());
}

#[test]
fn fallible_rust_allocations_retire_sources_and_preserve_healthy_key() {
    let _serial = serial();
    for count in 0..4 {
        let source = Source::new();
        let closed = source.closed.clone();
        assert!(matches!(
            budget(count, || Identity::generate(source)),
            Err(Error::Capacity)
        ));
        assert!(closed.load(Ordering::Acquire));
        assert!(Identity::generate(Source::new()).is_ok());
    }
    let identity = Identity::generate(Source::new()).unwrap();
    let mut output = [0xa5; 2048];
    assert_eq!(
        budget(0, || identity.request("device", &mut output)),
        Err(Error::Capacity)
    );
    assert!(output.iter().all(|v| *v == 0));
    assert!(identity.request("device", &mut output).is_ok());
}

#[cfg(feature = "probe")]
mod witness {
    use super::*;
    #[test]
    fn native_allocator_reuses_bounded_record_storage() {
        let _serial = serial();
        let identity = Identity::generate(Source::new()).unwrap();
        // SAFETY: use the paired SDK callbacks and never retain a freed slot.
        unsafe {
            let outside = tls_test_calloc(8192);
            assert!(!outside.is_null());
            let scope = identity.record.enter().unwrap();
            let first = tls_test_calloc(4096);
            let second = tls_test_calloc(4096);
            assert!(!first.is_null() && !second.is_null() && first != second);
            assert!(tls_test_calloc(8192).is_null());
            tls_test_free(first);
            let reused = tls_test_calloc(4096);
            assert_eq!(reused, first);
            tls_test_free(reused);
            tls_test_free(second);
            assert_eq!(scope.finish(), Err(Error::Capacity));
            let scope = identity.record.enter().unwrap();
            // An unexpected release uses its original allocator and fails the record.
            tls_test_free(outside);
            assert_eq!(scope.finish(), Err(Error::Crypto));
        }
    }

    #[test]
    fn actual_c_callback_clears_partial_entropy_and_estimate() {
        let _serial = serial();
        let source = Source::new();
        let fail = source.fail.clone();
        let identity = Identity::generate(source).unwrap();
        fail.store(true, Ordering::Release);
        let mut output = [0xa5; 128];
        let mut bits = 99;
        assert_eq!(
            identity.sdk(|| unsafe {
                tls_test_entropy_probe(&mut bits, output.as_mut_ptr(), output.len())
            }),
            Err(Error::Entropy)
        );
        assert_eq!(bits, 0);
        assert!(output.iter().all(|v| *v == 0));
    }
    #[test]
    fn nonexportable_key_and_real_exportable_positive_control() {
        let _serial = serial();
        let identity = Identity::generate(Source::new()).unwrap();
        let mut output = [0xa5; 256];
        let mut length = 99;
        assert_eq!(
            identity.sdk(|| unsafe {
                psa_export_key(identity.key, output.as_mut_ptr(), output.len(), &mut length)
            }),
            Err(Error::Crypto)
        );
        assert_eq!(length, 0);
        let attributes = unsafe {
            Opaque::new(
                tls_attributes_size(),
                tls_attributes_align(),
                tls_attributes_init,
                psa_reset_key_attributes,
            )
        }
        .unwrap();
        unsafe {
            tls_set_type(attributes.pointer(), tls_p256_type());
            tls_set_bits(attributes.pointer(), 256);
            tls_set_usage(attributes.pointer(), tls_test_export_usage());
            tls_set_algorithm(attributes.pointer(), tls_ecdsa_sha256());
        }
        let mut key = 0;
        identity
            .sdk(|| unsafe { psa_generate_key(attributes.pointer(), &mut key) })
            .unwrap();
        identity
            .sdk(|| unsafe { psa_export_key(key, output.as_mut_ptr(), output.len(), &mut length) })
            .unwrap();
        assert_eq!(length, 32);
        output.fill(0);
        assert_eq!(unsafe { psa_destroy_key(key) }, 0);
    }
}
