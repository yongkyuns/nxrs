//! SDK/Rust ABI witness. Fresh host OS entropy is a private per-image fixture;
//! no onboard RNG or production credential-storage claim is made.
#![forbid(unsafe_code)]

#[cfg(target_os = "nuttx")]
mod witness {
    use device_identity::{Entropy, EntropyUnavailable, Error, Identity};
    use std::cell::RefCell;
    use std::rc::Rc;

    static FIXTURE: &[u8] = include_bytes!(env!("NXRS_IDENTITY_ENTROPY_FIXTURE"));
    const _: () = assert!(FIXTURE.len() == 4096);
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
    fn request(identity: &mut Identity<'_>, label: &str) {
        let mut output = [0; 2048];
        let length = identity.request("cam_A", &mut output).expect("target CSR");
        let public = std::str::from_utf8(&output[..length]).expect("public PEM encoding");
        println!("NXRS_IDENTITY_CSR_BEGIN {label}");
        print!("{public}");
        println!("NXRS_IDENTITY_CSR_END {label}");
        output.fill(0);
    }
    pub fn run() {
        println!("NXRS_IDENTITY_TARGET_ENTERED");
        let pool = Rc::new(RefCell::new(Pool {
            bytes: FIXTURE.to_vec(),
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
        request(&mut first, "A");
        drop(first);
        let mut second = Identity::generate(Source(pool.clone())).expect("target key B");
        request(&mut second, "B");
        pool.borrow_mut().failed = true;
        assert_eq!(second.refresh_entropy(), Err(Error::Entropy));
        let mut output = [0xa5; 2048];
        assert_eq!(second.request("cam_A", &mut output), Err(Error::Poisoned));
        assert!(output.iter().all(|byte| *byte == 0));
        drop(second);
        pool.borrow_mut().failed = false;
        let restored = Identity::generate(Source(pool.clone())).expect("target restored owner");
        drop(restored);
        let consumed = pool.borrow().position;
        pool.borrow_mut().bytes.fill(0);
        println!("NXRS_IDENTITY_TARGET_REPORT requests=2 startup_refused=1 busy_refused=1 refresh_refused=1 sticky_refused=1 restored=1 entropy_bytes={consumed}");
    }
}

fn main() {
    #[cfg(target_os = "nuttx")]
    witness::run();
    #[cfg(not(target_os = "nuttx"))]
    panic!("This witness must execute inside its qualified NuttX image");
}
