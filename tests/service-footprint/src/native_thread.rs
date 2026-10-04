//! Diagnostic of typed Rust work on plain pthreads, not a std Thread substitute.
//! Handles always join (including on drop); no detach, park, TLS inheritance or
//! recoverable panic semantics. The target uses immediate-abort panic policy.
#![deny(unsafe_op_in_unsafe_fn)]

use std::ffi::{c_int, c_void};
use std::marker::PhantomData;
use std::mem::ManuallyDrop;
use std::ptr;

unsafe extern "C" {
    fn nxrs_cq_thread_start(
        handle: *mut *mut c_void,
        stack: usize,
        entry: extern "C" fn(*mut c_void) -> *mut c_void,
        argument: *mut c_void,
    ) -> c_int;
    fn nxrs_cq_thread_join(handle: *mut c_void, result: *mut *mut c_void) -> c_int;
}

pub struct JoinHandle<T> {
    handle: *mut c_void,
    result: PhantomData<T>,
}

pub fn spawn<F, T>(stack: usize, f: F) -> Result<JoinHandle<T>, ()>
where
    F: FnOnce() -> T + Send + 'static,
    T: Send + 'static,
{
    extern "C" fn entry<F: FnOnce() -> T, T>(argument: *mut c_void) -> *mut c_void {
        // SAFETY: spawn passes one Box<F>. C invokes this once iff start succeeds.
        // Panics cannot unwind across this ABI: they abort, rather than returning
        // a std panic payload. The joined caller takes ownership of Box<T>.
        let work = unsafe { Box::from_raw(argument.cast::<F>()) };
        Box::into_raw(Box::new(work())).cast()
    }
    let argument = Box::into_raw(Box::new(f));
    let mut handle = ptr::null_mut();
    // SAFETY: C uses configured pthread types, retains argument only on success
    // and creates at most one callback. F and T satisfy Send and 'static.
    let error = unsafe { nxrs_cq_thread_start(&mut handle, stack, entry::<F, T>, argument.cast()) };
    if error != 0 {
        // SAFETY: failure guarantees the callback was never started.
        drop(unsafe { Box::from_raw(argument) });
        Err(())
    } else {
        Ok(JoinHandle {
            handle,
            result: PhantomData,
        })
    }
}

impl<T> JoinHandle<T> {
    pub fn join(self) -> Result<T, ()> {
        // Prevent a second join in Drop. If native join fails, leaking the still
        // live native handle/result is safe; freeing either might not be.
        let this = ManuallyDrop::new(self);
        let mut result = ptr::null_mut();
        // SAFETY: this uniquely owns a successful spawn's live handle.
        if unsafe { nxrs_cq_thread_join(this.handle, &mut result) } != 0 {
            return Err(());
        }
        // SAFETY: pthread_join synchronizes with completion, and our trampoline
        // returned exactly one Box<T>. There is no cancellation API in this shim.
        Ok(*unsafe { Box::from_raw(result.cast::<T>()) })
    }
}

impl<T> Drop for JoinHandle<T> {
    fn drop(&mut self) {
        let mut result = ptr::null_mut();
        // SAFETY: Drop consumes the sole handle if join was not explicitly called.
        // On success the worker is stopped and its owned result can be dropped.
        if unsafe { nxrs_cq_thread_join(self.handle, &mut result) } == 0 {
            drop(unsafe { Box::from_raw(result.cast::<T>()) });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    struct CountDrop(Arc<AtomicUsize>);
    impl Drop for CountDrop {
        fn drop(&mut self) {
            self.0.fetch_add(1, Ordering::SeqCst);
        }
    }

    #[test]
    fn transfers_owned_noncopy_result() {
        let joined = spawn(65_536, || String::from("joined")).unwrap();
        assert_eq!(joined.join().unwrap(), "joined");
    }

    #[test]
    fn dropping_handle_joins_and_drops_result_once() {
        let count = Arc::new(AtomicUsize::new(0));
        let captured = CountDrop(count.clone());
        drop(spawn(65_536, move || captured).unwrap());
        assert_eq!(count.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn failed_spawn_drops_capture_once() {
        let count = Arc::new(AtomicUsize::new(0));
        let captured = CountDrop(count.clone());
        assert!(spawn(1, move || captured).is_err());
        assert_eq!(count.load(Ordering::SeqCst), 1);
    }
}
