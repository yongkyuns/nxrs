//! Shared task-context execution for bounded sensor event producers.
//! Device waits, cancellation mechanisms, errors and cleanup belong to the HAL.
#![forbid(unsafe_code)]

pub mod mock;
#[cfg(unix)]
pub mod readiness;

use messaging::DeliveryError;
use std::{
    io,
    sync::{
        atomic::{AtomicBool, AtomicU32, Ordering},
        Arc,
    },
    thread::{self, JoinHandle},
};

pub trait Wake: Send + Sync + 'static {
    fn wake(&self) -> io::Result<()>;
}

pub struct Worker<C: Wake, E: Copy> {
    cancel: C,
    stopping: Arc<AtomicBool>,
    dropped: Arc<AtomicU32>,
    join: Option<JoinHandle<Result<(), E>>>,
    result: Option<Result<(), E>>,
}
impl<C: Wake, E: Copy + Send + 'static> Worker<C, E> {
    pub fn start<
        T: Send + 'static,
        S: FnMut(T) -> messaging::DeliveryResult<T> + Send + 'static,
    >(
        name: &str,
        stack_bytes: usize,
        cancel: C,
        mut sink: S,
        failure: fn(E) -> T,
        run: impl FnOnce(&AtomicBool, &mut dyn FnMut(T) -> bool) -> Result<(), E> + Send + 'static,
    ) -> io::Result<Self> {
        let stopping = Arc::new(AtomicBool::new(false));
        let dropped = Arc::new(AtomicU32::new(0));
        let stop = stopping.clone();
        let losses = dropped.clone();
        let join = thread::Builder::new()
            .name(name.into())
            .stack_size(stack_bytes)
            .spawn(move || {
                let mut emit = |event| match sink(event) {
                    Ok(()) => true,
                    Err(rejected) if rejected.error == DeliveryError::Full => {
                        let _ = losses.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |n| {
                            Some(n.saturating_add(1))
                        });
                        true
                    }
                    Err(_) => false,
                };
                let result = run(&stop, &mut emit);
                if let Err(error) = result {
                    // Retain one terminal event until admitted, closed, or canceled.
                    // The join result independently retains the failure.
                    let mut pending = failure(error);
                    while !stop.load(Ordering::Acquire) {
                        match sink(pending) {
                            Ok(()) => break,
                            Err(rejected) if rejected.error == DeliveryError::Full => {
                                pending = rejected.value;
                                thread::park_timeout(std::time::Duration::from_millis(5));
                            }
                            Err(_) => break,
                        }
                    }
                }
                result
            })?;
        Ok(Self {
            cancel,
            stopping,
            dropped,
            join: Some(join),
            result: None,
        })
    }
    pub fn request_stop(&self) -> io::Result<()> {
        self.stopping.store(true, Ordering::Release);
        self.cancel.wake()
    }
    pub fn is_finished(&self) -> bool {
        self.join.as_ref().is_none_or(JoinHandle::is_finished)
    }
    pub fn dropped(&self) -> u32 {
        self.dropped.load(Ordering::Relaxed)
    }
    pub fn join(&mut self, panic_error: E) -> Result<(), E> {
        if let Some(join) = self.join.take() {
            self.result = Some(join.join().unwrap_or(Err(panic_error)));
        }
        self.result.unwrap_or(Ok(()))
    }
}
impl<C: Wake, E: Copy> Drop for Worker<C, E> {
    fn drop(&mut self) {
        self.stopping.store(true, Ordering::Release);
        let _ = self.cancel.wake();
        // No hidden join. The worker owns device resources until cleanup completes.
    }
}
