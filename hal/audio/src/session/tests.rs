use super::*;
use std::cell::Cell;

#[derive(Default)]
struct Trace {
    busy: Cell<bool>,
    read_failure: Cell<Option<DeviceError>>,
    submit_failure: Cell<Option<usize>>,
    reads: Cell<usize>,
    submits: Cell<usize>,
    retire_calls: Cell<usize>,
    dropped: Cell<bool>,
    retained: Cell<bool>,
}

struct Fake<'a>(&'a Trace);

impl Drop for Fake<'_> {
    fn drop(&mut self) {
        self.0.dropped.set(true);
    }
}

impl Source for Fake<'_> {
    fn start(&mut self) -> Result<(), DeviceError> {
        Ok(())
    }

    fn submit(&mut self, _: usize) -> Result<(), DeviceError> {
        let submitted = self.0.submits.get();
        if self.0.submit_failure.get() == Some(submitted) {
            return Err(DeviceError::Io);
        }
        self.0.submits.set(submitted + 1);
        Ok(())
    }

    fn completed(&mut self, _: Instant) -> Result<usize, DeviceError> {
        self.0.reads.set(self.0.reads.get() + 1);
        if let Some(error) = self.0.read_failure.get() {
            return Err(error);
        }
        Ok(0)
    }

    fn read_samples(
        &self,
        _: usize,
        samples: &mut [i16; SAMPLES_PER_BLOCK],
    ) -> Result<(), DeviceError> {
        samples.fill(-21);
        Ok(())
    }

    fn retire(&mut self) -> Result<(), DeviceError> {
        self.0.retire_calls.set(self.0.retire_calls.get() + 1);
        if self.0.busy.get() {
            Err(DeviceError::Busy)
        } else {
            Ok(())
        }
    }

    fn retain_for_retry(self) {
        self.0.retained.set(true);
        std::mem::forget(self);
    }
}

#[test]
fn reads_and_requeues_complete_blocks_with_a_sample_offset() {
    let trace = Trace::default();
    let mut session = Session::open(Fake(&trace)).unwrap();
    let mut samples = [0; SAMPLES_PER_BLOCK];
    for first_sample in [0, SAMPLES_PER_BLOCK as u64] {
        let started = Instant::now();
        let frame = session.read(started, &mut samples).unwrap();
        assert_eq!(frame.first_sample, first_sample);
        assert!(frame.observed_at >= started);
        assert_eq!(samples, [-21; SAMPLES_PER_BLOCK]);
    }
    assert_eq!(trace.submits.get(), BUFFER_COUNT + 2);
}

#[test]
fn allows_a_read_after_timeout_and_keeps_a_terminal_error_sticky() {
    let trace = Trace::default();
    let mut session = Session::open(Fake(&trace)).unwrap();
    let mut samples = [0; SAMPLES_PER_BLOCK];
    trace.read_failure.set(Some(DeviceError::Timeout));
    assert_eq!(
        session.read(Instant::now(), &mut samples),
        Err(DeviceError::Timeout)
    );
    trace.read_failure.set(None);
    assert_eq!(
        session
            .read(Instant::now(), &mut samples)
            .unwrap()
            .first_sample,
        0
    );

    trace.read_failure.set(Some(DeviceError::InvalidData));
    for _ in 0..2 {
        assert_eq!(
            session.read(Instant::now(), &mut samples),
            Err(DeviceError::InvalidData)
        );
    }
    assert_eq!(trace.reads.get(), 3);
}

#[test]
fn retains_the_source_on_stop_timeout_then_releases_it_after_retry() {
    let trace = Trace::default();
    let mut session = Session::open(Fake(&trace)).unwrap();
    trace.busy.set(true);
    assert_eq!(session.stop(Instant::now()), Err(DeviceError::Timeout));
    assert!(!trace.dropped.get());
    assert_eq!(
        session.read(Instant::now(), &mut [0; SAMPLES_PER_BLOCK]),
        Err(DeviceError::Closed)
    );

    trace.busy.set(false);
    session.stop(Instant::now()).unwrap();
    session.stop(Instant::now()).unwrap();
    assert!(trace.dropped.get());
    assert_eq!(trace.retire_calls.get(), 2);
}

#[test]
fn retires_a_partial_start_and_quarantines_an_unfinished_drop() {
    let partial = Trace::default();
    partial.submit_failure.set(Some(1));
    assert!(matches!(
        Session::open(Fake(&partial)),
        Err(DeviceError::Io)
    ));
    assert_eq!(partial.submits.get(), 1);
    assert_eq!(partial.retire_calls.get(), 1);
    assert!(partial.dropped.get());

    let stuck = Trace::default();
    let session = Session::open(Fake(&stuck)).unwrap();
    stuck.busy.set(true);
    drop(session);
    assert_eq!(stuck.retire_calls.get(), 1);
    assert!(!stuck.dropped.get());
    assert!(stuck.retained.get());
}

#[test]
fn extracts_signed_pcm_from_only_the_selected_slot() {
    let mut bytes = [0; SAMPLES_PER_BLOCK * 8];
    for slots in bytes.as_chunks_mut::<8>().0.iter_mut() {
        slots.copy_from_slice(&[0, 251, 0, 128, 0, 32, 255, 127]);
    }
    let mut samples = [0; SAMPLES_PER_BLOCK];
    decode_slots(&bytes, 0, &mut samples).unwrap();
    assert_eq!(samples, [i16::MIN; SAMPLES_PER_BLOCK]);
    decode_slots(&bytes, 1, &mut samples).unwrap();
    assert_eq!(samples, [i16::MAX; SAMPLES_PER_BLOCK]);
    assert_eq!(
        decode_slots(&bytes[..bytes.len() - 1], 0, &mut samples),
        Err(DeviceError::InvalidData)
    );
}
