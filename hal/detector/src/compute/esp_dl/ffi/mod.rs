use std::ptr::NonNull;
#[path = "../parallel.rs"]
mod parallel;
pub(crate) use parallel::Worker;
pub const INPUT_BYTES: usize = 224 * 224 * 3;
pub const OUTPUT_BYTES: usize = (28 * 28 + 14 * 14 + 7 * 7) * 33;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Model,
}
unsafe extern "C" {
    fn pico_create() -> *mut std::ffi::c_void;
    fn pico_destroy(engine: *mut std::ffi::c_void);
    fn pico_run(
        engine: *mut std::ffi::c_void,
        input: *const i8,
        output: *mut i8,
        dispatch: Option<Dispatch>,
        context: *mut std::ffi::c_void,
    ) -> i32;
    fn pico_partition(module: *mut std::ffi::c_void, args: *mut std::ffi::c_void);
}
type Dispatch = unsafe extern "C" fn(
    *mut std::ffi::c_void,
    *mut std::ffi::c_void,
    *mut std::ffi::c_void,
    *mut std::ffi::c_void,
) -> i32;
/// A synchronous SDK partition; only its designated output rows are writable.
pub(crate) struct Partition {
    module: NonNull<std::ffi::c_void>,
    args: NonNull<std::ffi::c_void>,
}
// SAFETY: only the SDK's disjoint Conv row partitions enter this type. Arguments
// remain alive in the native caller until Worker::run has acknowledged both halves.
unsafe impl Send for Partition {}
impl Partition {
    pub(crate) fn run(self) -> Result<(), Error> {
        // SAFETY: the graph and argument owner wait for this synchronous call.
        unsafe { pico_partition(self.module.as_ptr(), self.args.as_ptr()) };
        Ok(())
    }
}
unsafe extern "C" fn dispatch(
    context: *mut std::ffi::c_void,
    module: *mut std::ffi::c_void,
    first: *mut std::ffi::c_void,
    second: *mut std::ffi::c_void,
) -> i32 {
    let (Some(module), Some(first), Some(second)) = (
        NonNull::new(module),
        NonNull::new(first),
        NonNull::new(second),
    ) else {
        return -1;
    };
    // SAFETY: Engine::run supplies its exclusively borrowed worker for this
    // synchronous SDK callback. It is cleared before the native call returns.
    let parallel = unsafe { &mut *context.cast::<Parallel>() };
    if (parallel.dispatch)(
        &mut parallel.worker,
        Partition {
            module,
            args: first,
        },
        Partition {
            module,
            args: second,
        },
    )
    .is_ok()
    {
        0
    } else {
        -1
    }
}
type DispatchPair = fn(&mut Worker<Partition>, Partition, Partition) -> Result<(), Error>;
struct Parallel {
    worker: Worker<Partition>,
    dispatch: DispatchPair,
}
/// One exclusively owned graph. Optional native callbacks synchronously borrow
/// partition arguments; its Rust helper joins before those callbacks return.
pub struct Engine {
    pointer: NonNull<std::ffi::c_void>,
    parallel: Option<Parallel>,
    failed: bool,
}
// SAFETY: tensors/ops are exclusive; only synchronous disjoint SDK partitions
// share them. No callback borrow survives run. All access requires &mut self.
unsafe impl Send for Engine {}
impl Engine {
    pub fn new() -> Result<Self, Error> {
        // SAFETY: the SDK constructor needs no borrowed state.
        NonNull::new(unsafe { pico_create() })
            .map(|pointer| Self {
                pointer,
                parallel: None,
                failed: false,
            })
            .ok_or(Error::Model)
    }
    pub(crate) fn with_parallel(
        stack: usize,
        initialize: impl FnOnce() -> Result<(), Error> + Send + 'static,
        execute: fn(Partition) -> Result<(), Error>,
        dispatch: DispatchPair,
    ) -> Result<Self, Error> {
        let mut engine = Self::new()?;
        engine.parallel = Some(Parallel {
            worker: Worker::start(stack, initialize, execute)?,
            dispatch,
        });
        Ok(engine)
    }
    pub fn run(
        &mut self,
        input: &[i8; INPUT_BYTES],
        output: &mut [i8; OUTPUT_BYTES],
    ) -> Result<(), Error> {
        if self.failed {
            return Err(Error::Model);
        }
        let (callback, context) = match &mut self.parallel {
            Some(parallel) => (
                Some(dispatch as Dispatch),
                (parallel as *mut Parallel).cast(),
            ),
            None => (None, std::ptr::null_mut()),
        };
        // SAFETY: exact buffer sizes, no aliasing, and exclusive graph ownership.
        // The native call is finite and synchronous and retains neither buffer.
        match unsafe {
            pico_run(
                self.pointer.as_ptr(),
                input.as_ptr(),
                output.as_mut_ptr(),
                callback,
                context,
            )
        } {
            0 => Ok(()),
            _ => {
                self.failed = true;
                Err(Error::Model)
            }
        }
    }
}
impl Drop for Engine {
    fn drop(&mut self) {
        drop(self.parallel.take()); // join before freeing any graph/tensor storage
                                    // SAFETY: exactly one matching destructor; &mut ownership rules exclude
                                    // concurrent run, and the SDK never creates an asynchronous callback.
        unsafe { pico_destroy(self.pointer.as_ptr()) }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        sync::atomic::{AtomicUsize, Ordering},
        vec,
        vec::Vec,
    };
    #[test]
    fn partitioned_fixed_graph_matches_every_output_and_rejects_failed_work() {
        static SPLITS: AtomicUsize = AtomicUsize::new(0);
        let input: Vec<i8> = (0..INPUT_BYTES)
            .map(|i| ((i * 17 + i / 224) % 128) as i8)
            .collect();
        let input = input.as_slice().try_into().unwrap();
        let mut expected = vec![0; OUTPUT_BYTES];
        Engine::new()
            .unwrap()
            .run(input, expected.as_mut_slice().try_into().unwrap())
            .unwrap();
        let mut actual = vec![0; OUTPUT_BYTES];
        let mut engine = Engine::with_parallel(
            32768,
            || Ok(()),
            Partition::run,
            |worker, first, second| {
                SPLITS.fetch_add(1, Ordering::Relaxed);
                worker.run(first, second)
            },
        )
        .unwrap();
        engine
            .run(input, actual.as_mut_slice().try_into().unwrap())
            .unwrap();
        assert!(SPLITS.load(Ordering::Relaxed) > 0);
        assert_eq!(actual, expected); // includes the unaligned 14x14x1 score head
        drop(engine);
        let mut failed =
            Engine::with_parallel(32768, || Ok(()), |_| Err(Error::Model), Worker::run).unwrap();
        actual.fill(7);
        assert_eq!(
            failed.run(input, actual.as_mut_slice().try_into().unwrap()),
            Err(Error::Model)
        );
        assert!(actual.iter().all(|&value| value == 7)); // partial heads never published
    }
}
