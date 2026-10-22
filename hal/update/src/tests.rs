//! Resource/lifecycle fault controls. StubVerifier is never crypto evidence.
use crate::{
    controller::Stager,
    worker::{Flash, Verifier},
    Error, Policy, StagedImage, Trust, Update, Version,
};
use std::{
    boxed::Box,
    sync::{Arc, Condvar, Mutex},
    time::{Duration, Instant},
    vec,
    vec::Vec,
};

fn policy() -> Policy {
    Policy::new(
        &Trust {
            target: "fixture",
            policy_sha256: [0; 32],
            layout_sha256: [0; 32],
            configuration_sha256: [0; 32],
            key_sha256: [0; 32],
            layout: crate::Layout {
                flash_bytes: 65536,
                primary_offset: 4096,
                secondary_offset: 20480,
                slot_bytes: 16384,
                scratch_offset: 36864,
                scratch_bytes: 4096,
                erase_bytes: 4096,
                header_bytes: 32,
                write_align: 4,
                max_align: 8,
                max_sectors: 4,
            },
        },
        core::num::NonZeroU32::new(1).unwrap(),
    )
    .unwrap()
}
fn metadata() -> StagedImage {
    StagedImage {
        security_counter: core::num::NonZeroU32::new(7).unwrap(),
        version: Version {
            major: 1,
            minor: 0,
            revision: 0,
            build: 0,
        },
        payload_sha256: [9; 32],
    }
}
struct StubVerifier(bool);
impl Verifier for StubVerifier {
    fn authenticate(&mut self, _: &[u8], _: &Policy) -> Result<StagedImage, Error> {
        if self.0 {
            Ok(metadata())
        } else {
            Err(Error::Authentication)
        }
    }
}
#[derive(Default)]
struct Gate {
    state: Mutex<(bool, bool)>,
    changed: Condvar,
}
impl Gate {
    fn enter(&self) {
        let mut state = self.state.lock().unwrap();
        state.0 = true;
        self.changed.notify_all();
        while !state.1 {
            state = self.changed.wait(state).unwrap();
        }
    }
    fn entered(&self) {
        let end = Instant::now() + Duration::from_secs(3);
        let mut state = self.state.lock().unwrap();
        while !state.0 {
            let (next, timeout) = self
                .changed
                .wait_timeout(state, end.saturating_duration_since(Instant::now()))
                .unwrap();
            state = next;
            assert!(
                !timeout.timed_out() || state.0,
                "worker never entered native call"
            );
        }
    }
    fn release(&self) {
        let mut state = self.state.lock().unwrap();
        state.1 = true;
        self.changed.notify_all();
    }
}
struct State {
    primary: Vec<u8>,
    secondary: Vec<u8>,
    scratch: Vec<u8>,
    erases: Vec<u32>,
    writes: usize,
    closed: usize,
    dropped: usize,
    corrupt: bool,
    close_fail: bool,
}
struct Fake {
    state: Arc<Mutex<State>>,
    gate: Option<Arc<Gate>>,
    gate_write: bool,
}
impl Drop for Fake {
    fn drop(&mut self) {
        self.state.lock().unwrap().dropped += 1;
    }
}
impl Flash for Fake {
    fn validate(&mut self, _: &Policy) -> Result<(), Error> {
        Ok(())
    }
    fn read_primary(&mut self, o: u32, b: &mut [u8]) -> Result<(), Error> {
        let s = self.state.lock().unwrap();
        b.copy_from_slice(&s.primary[o as usize..o as usize + b.len()]);
        Ok(())
    }
    fn read_secondary(&mut self, o: u32, b: &mut [u8]) -> Result<(), Error> {
        let s = self.state.lock().unwrap();
        b.copy_from_slice(&s.secondary[o as usize..o as usize + b.len()]);
        if s.corrupt && s.writes > 0 && o == 0 {
            b[0] ^= 1;
        }
        Ok(())
    }
    fn read_scratch(&mut self, o: u32, b: &mut [u8]) -> Result<(), Error> {
        let s = self.state.lock().unwrap();
        b.copy_from_slice(&s.scratch[o as usize..o as usize + b.len()]);
        Ok(())
    }
    fn erase_secondary(&mut self, block: u32) -> Result<(), Error> {
        if !self.gate_write {
            if let Some(gate) = self.gate.take() {
                gate.enter();
            }
        }
        let mut s = self.state.lock().unwrap();
        s.erases.push(block);
        s.secondary[block as usize * 4096..(block as usize + 1) * 4096].fill(255);
        Ok(())
    }
    fn write_secondary(&mut self, o: u32, b: &[u8]) -> Result<(), Error> {
        if self.gate_write {
            if let Some(gate) = self.gate.take() {
                gate.enter();
            }
        }
        let mut s = self.state.lock().unwrap();
        let target = &mut s.secondary[o as usize..o as usize + b.len()];
        if target.iter().zip(b).any(|(a, b)| a & b != *b) {
            return Err(Error::Io);
        }
        target.copy_from_slice(b);
        s.writes += 1;
        Ok(())
    }
    fn close(&mut self) -> Result<(), Error> {
        let mut s = self.state.lock().unwrap();
        s.closed += 1;
        if s.close_fail {
            Err(Error::Io)
        } else {
            Ok(())
        }
    }
}
fn owner(accept: bool, gate: Option<Arc<Gate>>, gate_write: bool) -> (Stager, Arc<Mutex<State>>) {
    let state = Arc::new(Mutex::new(State {
        primary: vec![255; 16384],
        secondary: vec![255; 16384],
        scratch: vec![255; 4096],
        erases: Vec::new(),
        writes: 0,
        closed: 0,
        dropped: 0,
        corrupt: false,
        close_fail: false,
    }));
    let flash = Fake {
        state: Arc::clone(&state),
        gate,
        gate_write,
    };
    (
        Stager::spawn(
            flash,
            StubVerifier(accept),
            policy(),
            256 * 1024,
            Duration::from_millis(30),
            || {},
        )
        .unwrap(),
        state,
    )
}
fn image() -> Box<[u8]> {
    vec![0x5a; 5001].into_boxed_slice()
}
fn outcome(owner: &mut Stager) -> Update {
    let end = Instant::now() + Duration::from_secs(3);
    loop {
        let event = owner.poll();
        if event != Update::None {
            return event;
        }
        assert!(Instant::now() < end, "missing outcome");
        std::thread::yield_now();
    }
}
fn close(owner: &mut Stager) {
    owner.begin_shutdown();
    let end = Instant::now() + Duration::from_secs(3);
    loop {
        if owner.poll_shutdown().unwrap() {
            return;
        }
        assert!(Instant::now() < end, "missing retirement");
        std::thread::yield_now();
    }
}

#[test]
fn authenticated_stage_erases_trailer_first_preserves_primary_and_never_selects() {
    let (mut owner, state) = owner(true, None, false);
    let id = owner.stage(image(), Duration::from_secs(2)).unwrap();
    assert_eq!(
        outcome(&mut owner),
        Update::Completed {
            id,
            result: Ok(metadata()),
            quiescent: true
        }
    );
    assert_eq!(
        owner
            .stage(image(), Duration::from_secs(2))
            .unwrap_err()
            .error,
        Error::Busy
    );
    assert_eq!(outcome(&mut owner), Update::Quiescent { id });
    let s = state.lock().unwrap();
    assert_eq!(s.erases, [3, 0, 1, 2]);
    assert_eq!(s.writes, 2);
    assert!(s.primary.iter().all(|b| *b == 255));
    assert_eq!(&s.secondary[..5001], &*image());
    assert!(s.secondary[5001..].iter().all(|b| *b == 255));
    drop(s);
    close(&mut owner);
    assert_eq!(state.lock().unwrap().closed, 1);
}
#[test]
fn authentication_refusal_has_no_mutation_and_allows_a_later_request() {
    let (mut owner, state) = owner(false, None, false);
    for _ in 0..2 {
        let id = owner.stage(image(), Duration::from_secs(2)).unwrap();
        assert_eq!(
            outcome(&mut owner),
            Update::Completed {
                id,
                result: Err(Error::Authentication),
                quiescent: true
            }
        );
        assert_eq!(outcome(&mut owner), Update::Quiescent { id });
    }
    let s = state.lock().unwrap();
    assert!(s.erases.is_empty());
    assert_eq!(s.writes, 0);
    drop(s);
    close(&mut owner);
}
#[test]
fn trial_primary_and_pending_or_partial_secondary_control_states_are_untouched() {
    for target in 0..4 {
        let (mut owner, state) = owner(true, None, false);
        {
            let mut s = state.lock().unwrap();
            if target == 0 {
                s.primary[16368..].copy_from_slice(&crate::protocol::MAGIC);
                s.primary[16352] = 1;
            } else if target == 1 {
                s.secondary[16368..].copy_from_slice(&crate::protocol::MAGIC);
            } else if target == 2 {
                s.secondary[16383] = 0;
            } else {
                s.scratch[4095] = 0;
            }
        }
        let id = owner.stage(image(), Duration::from_secs(2)).unwrap();
        assert_eq!(
            outcome(&mut owner),
            Update::Completed {
                id,
                result: Err(Error::BootBusy),
                quiescent: true
            }
        );
        let s = state.lock().unwrap();
        assert!(s.erases.is_empty());
        assert_eq!(s.writes, 0);
        drop(s);
        close(&mut owner);
    }
}
#[test]
fn cancel_reports_once_and_retains_ownership_until_native_return_even_after_drop() {
    let gate = Arc::new(Gate::default());
    let (mut owner, state) = owner(true, Some(Arc::clone(&gate)), false);
    let id = owner.stage(image(), Duration::from_secs(2)).unwrap();
    gate.entered();
    owner.cancel(id).unwrap();
    assert_eq!(
        outcome(&mut owner),
        Update::Completed {
            id,
            result: Err(Error::Cancelled),
            quiescent: false
        }
    );
    assert_eq!(
        owner
            .stage(image(), Duration::from_secs(2))
            .unwrap_err()
            .error,
        Error::Busy
    );
    drop(owner);
    assert_eq!(state.lock().unwrap().closed, 0);
    assert_eq!(state.lock().unwrap().dropped, 0);
    gate.release();
    let end = Instant::now() + Duration::from_secs(3);
    while state.lock().unwrap().dropped == 0 {
        assert!(Instant::now() < end);
        std::thread::yield_now();
    }
    let s = state.lock().unwrap();
    assert_eq!(s.closed, 1);
    assert_eq!(s.erases.len(), 1);
    assert_eq!(s.writes, 0);
}
#[test]
fn operation_and_retirement_deadlines_are_sticky_without_freeing_inflight_io() {
    let gate = Arc::new(Gate::default());
    let (mut owner, state) = owner(true, Some(Arc::clone(&gate)), false);
    let id = owner.stage(image(), Duration::from_millis(50)).unwrap();
    gate.entered();
    std::thread::sleep(Duration::from_millis(60));
    assert_eq!(
        outcome(&mut owner),
        Update::Completed {
            id,
            result: Err(Error::TimedOut),
            quiescent: false
        }
    );
    std::thread::sleep(Duration::from_millis(40));
    assert_eq!(owner.poll(), Update::None);
    assert_eq!(owner.status().fault, Some(Error::TimedOut));
    assert_eq!(state.lock().unwrap().closed, 0);
    gate.release();
    assert_eq!(outcome(&mut owner), Update::Quiescent { id });
    assert_eq!(
        owner
            .stage(image(), Duration::from_secs(2))
            .unwrap_err()
            .error,
        Error::TimedOut
    );
    close(&mut owner);
}
#[test]
fn shutdown_during_write_waits_for_buffer_quiescence() {
    let gate = Arc::new(Gate::default());
    let (mut owner, state) = owner(true, Some(Arc::clone(&gate)), true);
    owner.stage(image(), Duration::from_secs(2)).unwrap();
    gate.entered();
    owner.begin_shutdown();
    assert!(!owner.poll_shutdown().unwrap());
    assert_eq!(state.lock().unwrap().closed, 0);
    gate.release();
    close(&mut owner);
    assert!(state.lock().unwrap().dropped > 0);
}
#[test]
fn corruption_is_sticky_and_failed_close_never_reports_retirement() {
    let (mut owner, state) = owner(true, None, false);
    state.lock().unwrap().corrupt = true;
    let id = owner.stage(image(), Duration::from_secs(2)).unwrap();
    assert_eq!(
        outcome(&mut owner),
        Update::Completed {
            id,
            result: Err(Error::Readback),
            quiescent: true
        }
    );
    assert_eq!(outcome(&mut owner), Update::Quiescent { id });
    assert_eq!(owner.status().fault, Some(Error::Readback));
    state.lock().unwrap().close_fail = true;
    owner.begin_shutdown();
    let end = Instant::now() + Duration::from_secs(3);
    loop {
        match owner.poll_shutdown() {
            Err(Error::Io) => break,
            Ok(false) => {}
            other => panic!("wrong retirement: {other:?}"),
        };
        assert!(Instant::now() < end);
        std::thread::yield_now();
    }
    assert!(!owner.status().retired);
    assert_eq!(owner.status().fault, Some(Error::Readback));
    assert_eq!(state.lock().unwrap().dropped, 0);
}
#[test]
fn explicit_mock_factory_does_not_supply_an_authenticating_fallback() {
    struct RejectCrypto;
    impl crate::Crypto for RejectCrypto {
        type Error = ();
        fn sha256(&mut self, _: &[u8]) -> Result<[u8; 32], ()> {
            Err(())
        }
        fn verify_rsa3072_pss_sha256(&mut self, _: &[u8; 32], _: &[u8; 384]) -> Result<(), ()> {
            Err(())
        }
    }
    let mut owner =
        crate::mock::open(policy(), RejectCrypto, Duration::from_secs(1), || {}).unwrap();
    let id = owner.stage(image(), Duration::from_secs(2)).unwrap();
    assert_eq!(
        outcome(&mut owner),
        Update::Completed {
            id,
            result: Err(Error::Authentication),
            quiescent: true
        }
    );
    close(&mut owner);
}

#[test]
fn primary_confirmation_without_complete_known_swap_state_is_refused() {
    let mut tail = [255; crate::protocol::TAIL];
    tail[32..].copy_from_slice(&crate::protocol::MAGIC);
    tail[24] = 1;
    assert!(!crate::protocol::idle_primary(&tail, 16384));
    tail[16] = 1;
    assert!(!crate::protocol::idle_primary(&tail, 16384));
    tail[..4].copy_from_slice(&4096u32.to_le_bytes());
    tail[8] = 3;
    assert!(crate::protocol::idle_primary(&tail, 16384));
    for (offset, value) in [
        (4, 0),
        (8, 0x13),
        (9, 0),
        (16, 255),
        (17, 0),
        (24, 255),
        (25, 0),
        (32, 0),
    ] {
        let mut corrupt = tail;
        corrupt[offset] = value;
        assert!(!crate::protocol::idle_primary(&corrupt, 16384));
    }
    for size in [0u32, 16385, u32::MAX] {
        tail[..4].copy_from_slice(&size.to_le_bytes());
        assert!(!crate::protocol::idle_primary(&tail, 16384));
    }
}
