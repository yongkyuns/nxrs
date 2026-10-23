//! SDK/Rust ABI witness. Fresh host OS entropy is a private per-image fixture;
//! no onboard RNG or production credential-storage claim is made.
#![forbid(unsafe_code)]

#[cfg_attr(not(target_os = "nuttx"), allow(dead_code))]
mod witness {
    use device_identity::{
        CameraCertificatePolicy, Entropy, EntropyUnavailable, Error, Identity, RevocationState,
    };
    use std::cell::RefCell;
    use std::io::{BufRead, Read, Write};
    use std::rc::Rc;

    use std::sync::mpsc;
    use std::thread;
    use std::time::{Duration, Instant};
    struct Pool {
        bytes: Vec<u8>,
        position: usize,
        failed: bool,
    }
    struct Source(Rc<RefCell<Pool>>);
    impl Entropy for Source {
        fn fill(&mut self, output: &mut [u8]) -> Result<(), EntropyUnavailable> {
            let mut pool = self.0.try_borrow_mut().map_err(|_| EntropyUnavailable)?;
            if pool.failed {
                return Err(EntropyUnavailable);
            }
            let end = pool
                .position
                .checked_add(output.len())
                .ok_or(EntropyUnavailable)?;
            if end > pool.bytes.len() {
                return Err(EntropyUnavailable);
            }
            let start = pool.position;
            output.copy_from_slice(&pool.bytes[start..end]);
            pool.bytes[start..end].fill(0);
            pool.position = end;
            Ok(())
        }
    }
    fn line(input: &mut impl BufRead) -> String {
        let mut value = String::new();
        let length = (&mut *input)
            .take(130)
            .read_line(&mut value)
            .expect("certificate UART line");
        assert!(length > 0 && length < 130 && value.ends_with('\n'));
        value.trim_end_matches(['\r', '\n']).to_owned()
    }
    fn read_der(input: &mut impl BufRead) -> Vec<u8> {
        let header = line(input);
        let value = header
            .strip_prefix("NXRS_CERT_DER ")
            .expect("certificate frame header");
        let length: usize = value.parse().expect("certificate frame length");
        assert!(length > 0 && length <= 2048 && value == length.to_string());
        let mut output = Vec::with_capacity(length);
        while output.len() < length {
            let hex = line(input);
            assert!(!hex.is_empty() && hex.len() <= 64 && hex.len().is_multiple_of(2));
            assert!(
                output.len() + hex.len() / 2 <= length
                    && hex.bytes().all(|b| b.is_ascii_hexdigit())
            );
            let digit = |value: u8| match value.to_ascii_lowercase() {
                b'0'..=b'9' => value - b'0',
                b'a'..=b'f' => value.to_ascii_lowercase() - b'a' + 10,
                _ => panic!("certificate hex digit"),
            };
            for pair in hex.as_bytes().chunks_exact(2) {
                output.push(digit(pair[0]) * 16 + digit(pair[1]));
            }
        }
        output
    }
    #[cfg(test)]
    #[test]
    fn der_framing_enforces_exact_lengths_and_line_budgets() {
        let mut good = std::io::Cursor::new(b"NXRS_CERT_DER 3\n00a1ff\nremaining");
        assert_eq!(read_der(&mut good), vec![0, 0xa1, 0xff]);
        assert_eq!(&good.get_ref()[good.position() as usize..], b"remaining");
        for bad in [
            "NXRS_CERT_DER 0\n",
            "NXRS_CERT_DER 2049\n",
            "NXRS_CERT_DER +1\n00\n",
            "NXRS_CERT_DER 01\n00\n",
            "NXRS_CERT_DER 1\n0000\n",
            "NXRS_CERT_DER 1\nxx\n",
            "NXRS_CERT_DER 1\n0\n",
            "NXRS_CERT_DER 1\n",
            "NXRS_CERT_DER 1\n\n",
        ] {
            assert!(
                std::panic::catch_unwind(|| read_der(&mut std::io::Cursor::new(bad.as_bytes())))
                    .is_err()
            );
        }
        let oversized = format!("NXRS_CERT_DER 1\n{}\n", "0".repeat(130));
        assert!(
            std::panic::catch_unwind(|| read_der(&mut std::io::Cursor::new(oversized.as_bytes())))
                .is_err()
        );
    }
    fn policy(pin: &[u8; 32], now: u64) -> CameraCertificatePolicy<'static> {
        CameraCertificatePolicy {
            issuer_sha256: *pin,
            site_id: "site_a",
            camera_id: "cam_a",
            server_ip: [127, 0, 0, 1],
            trusted_unix_seconds: now,
        }
    }
    fn revocation(
        identity: &Identity<'_>,
        label: &str,
        root: &[u8],
        pin: &[u8; 32],
        now: u64,
        leaf: &[u8],
        crls: &[Vec<u8>],
    ) -> usize {
        assert_eq!(crls.len(), 6);
        let [clear, resigned, newer, revoked, conflict, short] = crls else {
            unreachable!()
        };
        // Initial issuer generation is explicitly fixed by the qualification
        // profile. UART cannot supply a trusted floor or change the root/time.
        // This is a fixture, not acquisition of protected production state.
        let state = RevocationState::from_trusted_floor(*pin, 1).unwrap();
        let started = Instant::now();
        let bound = identity
            .bind_current_certificate(leaf, root, policy(pin, now), &state, clear)
            .expect("target current certificate");
        let generation = bound.generation().unwrap();
        assert_eq!(bound.certificate_der().unwrap(), leaf);
        let binding_ms = started.elapsed().as_millis() as u64;
        let remaining = bound.remaining_validity().unwrap();
        assert_eq!(state.observe(identity, root, resigned, now), Ok(generation));
        assert!(bound.remaining_validity().unwrap() <= remaining);
        let mut refusals = 0;
        let mut altered = clear.clone();
        *altered.last_mut().unwrap() ^= 1;
        let mut trailing = clear.clone();
        trailing.push(0);
        for candidate in [
            &altered[..],
            &clear[..0],
            &clear[..clear.len() / 2],
            &clear[..clear.len() - 1],
            &trailing[..],
        ] {
            assert!(state.observe(identity, root, candidate, now).is_err());
            assert_eq!(bound.certificate_der().unwrap(), leaf);
            refusals += 1;
        }
        let mut wrong = policy(pin, now);
        wrong.issuer_sha256[0] ^= 1;
        assert!(identity
            .bind_current_certificate(leaf, root, wrong, &state, clear)
            .is_err());
        assert!(bound.certificate_der().is_ok());
        refusals += 1;
        assert_eq!(
            state.observe(identity, root, newer, now),
            Ok(generation + 1)
        );
        assert_eq!(bound.certificate_der(), Err(Error::StaleTrust));
        assert_eq!(
            state.observe(identity, root, clear, now),
            Err(Error::Rollback)
        );
        refusals += 1;
        let current = identity
            .bind_current_certificate(leaf, root, policy(pin, now), &state, newer)
            .expect("target refreshed current certificate");
        assert!(matches!(
            identity.bind_current_certificate(leaf, root, policy(pin, now), &state, revoked),
            Err(Error::Revoked)
        ));
        refusals += 1;
        assert_eq!(current.certificate_der(), Err(Error::StaleTrust));
        assert_eq!(
            state.observe(identity, root, clear, now),
            Err(Error::Rollback)
        );
        refusals += 1;
        let floor = RevocationState::from_trusted_floor(*pin, 1).unwrap();
        let floor_binding = identity
            .bind_current_certificate(leaf, root, policy(pin, now), &floor, clear)
            .unwrap();
        floor.raise_trusted_floor(generation + 1).unwrap();
        assert_eq!(floor_binding.certificate_der(), Err(Error::StaleTrust));
        assert_eq!(floor.raise_trusted_floor(generation), Err(Error::Rollback));
        refusals += 1;
        let equivocation = RevocationState::from_trusted_floor(*pin, 1).unwrap();
        let conflict_binding = identity
            .bind_current_certificate(leaf, root, policy(pin, now), &equivocation, clear)
            .unwrap();
        assert_eq!(
            equivocation.observe(identity, root, conflict, now),
            Err(Error::Poisoned)
        );
        refusals += 1;
        assert_eq!(conflict_binding.certificate_der(), Err(Error::Poisoned));
        refusals += 1;
        assert_eq!(
            equivocation.observe(identity, root, clear, now),
            Err(Error::Poisoned)
        );
        refusals += 1;
        let expiry = RevocationState::from_trusted_floor(*pin, 1).unwrap();
        let started = Instant::now();
        let expiring = identity
            .bind_current_certificate(leaf, root, policy(pin, now), &expiry, short)
            .expect("short CRL initially current");
        let short_binding_ms = started.elapsed().as_millis() as u64;
        assert!(expiring.certificate_der().is_ok());
        let wait = expiring.remaining_validity().unwrap() + Duration::from_millis(100);
        let expiry_wait_ms = wait.as_millis() as u64;
        thread::sleep(wait);
        assert_eq!(expiring.certificate_der(), Err(Error::Expired));
        // Replaying valid signed contents with the frozen UTC fixture cannot
        // replace its original, expired monotonic deadline.
        assert_eq!(expiry.observe(identity, root, short, now), Ok(generation));
        assert_eq!(expiring.certificate_der(), Err(Error::Expired));
        assert!(matches!(
            identity.bind_current_certificate(leaf, root, policy(pin, now), &expiry, short),
            Err(Error::Expired)
        ));
        refusals += 1;
        assert_eq!(refusals, 14);
        println!("NXRS_IDENTITY_REVOCATION_MEASURE {label} bind_ms={binding_ms} short_bind_ms={short_binding_ms} expiry_wait_ms={expiry_wait_ms}");
        println!("NXRS_IDENTITY_REVOCATION_BOUND {label} admitted=5 generation={generation} refusals={refusals} invalidated=3 revoked_refused=1 equivocation_closed=1 expiry_checked=1 replay_closed=1");
        refusals
    }
    fn request(
        identity: &mut Identity<'_>,
        label: &str,
        root: &[u8],
        pin: &[u8; 32],
        now: u64,
    ) -> (Vec<u8>, Vec<u8>, usize, usize) {
        let mut output = [0; 2048];
        let length = identity.request("cam_A", &mut output).expect("target CSR");
        let public = std::str::from_utf8(&output[..length]).expect("public PEM encoding");
        println!("NXRS_IDENTITY_CSR_BEGIN {label}");
        print!("{public}");
        println!("NXRS_IDENTITY_CSR_END {label}");
        output.fill(0);
        println!("NXRS_IDENTITY_CERTIFICATE_READY {label}");
        std::io::stdout()
            .flush()
            .expect("certificate readiness flush");
        let mut input = std::io::stdin().lock();
        let leaf = read_der(&mut input);
        let wrong = read_der(&mut input);
        let short = read_der(&mut input);
        let crls: Vec<Vec<u8>> = (0..6).map(|_| read_der(&mut input)).collect();
        drop(input);
        let binding_started = Instant::now();
        let bound = identity
            .bind_certificate(&leaf, root, policy(pin, now))
            .expect("target certificate for held key");
        assert_eq!(bound.certificate_der().unwrap(), leaf);
        assert!(bound.remaining_validity().unwrap() <= Duration::from_secs(300));
        let binding_ms = binding_started.elapsed().as_millis() as u64;
        drop(bound);
        let mut refusals = 0;
        assert!(identity
            .bind_certificate(&wrong, root, policy(pin, now))
            .is_err());
        refusals += 1;
        let mut bad = leaf.clone();
        *bad.last_mut().unwrap() ^= 1;
        assert!(identity
            .bind_certificate(&bad, root, policy(pin, now))
            .is_err());
        refusals += 1;
        for variant in 0..7 {
            let mut expected = policy(pin, now);
            match variant {
                0 => expected.issuer_sha256[0] ^= 1,
                1 => expected.camera_id = "other",
                2 => expected.site_id = "other",
                3 => expected.server_ip = [127, 0, 0, 2],
                4 => expected.trusted_unix_seconds = now + 8 * 86400,
                5 => expected.trusted_unix_seconds = 0,
                _ => expected.trusted_unix_seconds = now - 600,
            }
            assert!(identity.bind_certificate(&leaf, root, expected).is_err());
            refusals += 1;
        }
        for length in [0, leaf.len() / 2, leaf.len() - 1] {
            assert!(identity
                .bind_certificate(&leaf[..length], root, policy(pin, now))
                .is_err());
            refusals += 1;
        }
        bad = leaf.clone();
        bad.push(0);
        assert!(identity
            .bind_certificate(&bad, root, policy(pin, now))
            .is_err());
        refusals += 1;
        let short_started = Instant::now();
        let short = identity
            .bind_certificate(&short, root, policy(pin, now))
            .expect("short valid target certificate");
        assert!(short.certificate_der().is_ok());
        let short_binding_ms = short_started.elapsed().as_millis() as u64;
        let expiry_wait = short.remaining_validity().unwrap() + Duration::from_millis(100);
        let expiry_wait_ms = expiry_wait.as_millis() as u64;
        thread::sleep(expiry_wait);
        assert_eq!(short.certificate_der(), Err(Error::Expired));
        drop(short);
        assert_eq!(refusals, 13);
        println!("NXRS_IDENTITY_CERTIFICATE_MEASURE {label} bind_ms={binding_ms} short_bind_ms={short_binding_ms} expiry_wait_ms={expiry_wait_ms}");
        println!("NXRS_IDENTITY_CERTIFICATE_BOUND {label} refusals={refusals} expiry_checked=1");
        let revocation_refusals = revocation(identity, label, root, pin, now, &leaf, &crls);
        (leaf, crls[3].clone(), refusals, revocation_refusals)
    }
    pub fn run(fixture: &[u8; 4096], root: &[u8], pin: &[u8; 32], clock: &[u8; 8]) {
        let now = u64::from_le_bytes(*clock);
        println!("NXRS_IDENTITY_TARGET_ENTERED");
        let pool = Rc::new(RefCell::new(Pool {
            bytes: fixture.to_vec(),
            position: 0,
            failed: true,
        }));
        assert!(matches!(
            Identity::generate(Source(pool.clone())),
            Err(Error::Entropy)
        ));
        pool.borrow_mut().failed = false;
        let mut first = Identity::generate(Source(pool.clone())).expect("target key A");
        assert!(first.entropy_calls() > 0);
        assert!(matches!(
            Identity::generate(Source(pool.clone())),
            Err(Error::Busy)
        ));
        // Keep the non-Send owner on this thread. A real second NuttX thread
        // must refuse admission before it can touch SDK global state/entropy.
        let started = Instant::now();
        let (sender, receiver) = mpsc::sync_channel(1);
        let worker = thread::spawn(move || {
            let unavailable = Rc::new(RefCell::new(Pool {
                bytes: Vec::new(),
                position: 0,
                failed: true,
            }));
            sender
                .send(matches!(
                    Identity::generate(Source(unavailable)),
                    Err(Error::Busy)
                ))
                .expect("thread ownership result");
        });
        assert!(receiver
            .recv_timeout(Duration::from_secs(2))
            .expect("thread ownership deadline"));
        worker.join().expect("thread ownership join");
        assert!(started.elapsed() < Duration::from_secs(5));
        let (_, _, first_refusals, first_revocation_refusals) =
            request(&mut first, "A", root, pin, now);
        drop(first);
        let mut second = Identity::generate(Source(pool.clone())).expect("target key B");
        let (second_certificate, second_crl, second_refusals, second_revocation_refusals) =
            request(&mut second, "B", root, pin, now);
        pool.borrow_mut().failed = true;
        assert_eq!(second.refresh_entropy(), Err(Error::Entropy));
        let mut output = [0xa5; 2048];
        assert_eq!(second.request("cam_A", &mut output), Err(Error::Poisoned));
        assert!(output.iter().all(|byte| *byte == 0));
        assert!(matches!(
            second.bind_certificate(&second_certificate, root, policy(pin, now)),
            Err(Error::Poisoned)
        ));
        let state = RevocationState::from_trusted_floor(*pin, 1).unwrap();
        assert!(matches!(
            second.bind_current_certificate(
                &second_certificate,
                root,
                policy(pin, now),
                &state,
                &second_crl
            ),
            Err(Error::Poisoned)
        ));
        drop(second);
        pool.borrow_mut().failed = false;
        let restored = Identity::generate(Source(pool.clone())).expect("target restored owner");
        drop(restored);
        let consumed = pool.borrow().position;
        pool.borrow_mut().bytes.fill(0);
        let certificate_refusals = first_refusals + second_refusals;
        let revocation_refusals = first_revocation_refusals + second_revocation_refusals;
        println!("NXRS_IDENTITY_TARGET_REPORT requests=2 startup_refused=1 busy_refused=1 parallel_busy_refused=1 thread_joined=1 deadline_checked=1 refresh_refused=1 sticky_refused=1 restored=1 certificates_bound=2 certificate_refusals={certificate_refusals} expired_bindings=2 poisoned_certificate_refused=1 current_bindings=10 revocation_refusals={revocation_refusals} revoked_refused=2 invalidated_bindings=6 equivocation_closed=2 expired_crls=2 replay_closed=2 poisoned_revocation_refused=1 entropy_bytes={consumed}");
    }
}

fn main() {
    #[cfg(target_os = "nuttx")]
    witness::run(
        include_bytes!(env!("NXRS_IDENTITY_ENTROPY_FIXTURE")),
        include_bytes!(concat!(
            env!("NXRS_IDENTITY_CERTIFICATE_FIXTURE"),
            "/root.der"
        )),
        include_bytes!(concat!(
            env!("NXRS_IDENTITY_CERTIFICATE_FIXTURE"),
            "/pin.bin"
        )),
        include_bytes!(concat!(
            env!("NXRS_IDENTITY_CERTIFICATE_FIXTURE"),
            "/time.bin"
        )),
    );
    #[cfg(not(target_os = "nuttx"))]
    panic!("This witness must execute inside its qualified NuttX image");
}
