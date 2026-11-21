//! Task-context worker lifetimes, cancellation and native readiness.
#![deny(unsafe_code)]

#[cfg(target_os = "nuttx")]
mod platform;
#[cfg(target_os = "nuttx")]
pub use platform::nuttx;

pub mod mock;
pub mod probe;
#[cfg(unix)]
pub mod readiness;

use std::{
    io,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    thread::{self, JoinHandle},
};

pub trait Wake: Send + Sync + 'static {
    fn wake(&self) -> io::Result<()>;
}

struct State<S> {
    stopping: AtomicBool,
    caller: S,
}

pub struct Worker<C: Wake, E: Copy, S = ()> {
    cancel: C,
    state: Arc<State<S>>,
    join: Option<JoinHandle<Result<(), E>>>,
    result: Option<Result<(), E>>,
}
impl<C: Wake, E: Copy + Send + 'static, S: Send + Sync + 'static> Worker<C, E, S> {
    /// Share caller-owned state with the task for the lifetime of the worker.
    pub fn start(
        name: &str,
        stack_bytes: usize,
        cancel: C,
        caller: S,
        run: impl FnOnce(&AtomicBool, &S) -> Result<(), E> + Send + 'static,
    ) -> io::Result<Self> {
        let state = Arc::new(State {
            stopping: AtomicBool::new(false),
            caller,
        });
        let shared = state.clone();
        let join = thread::Builder::new()
            .name(name.into())
            .stack_size(stack_bytes)
            .spawn(move || run(&shared.stopping, &shared.caller))?;
        Ok(Self {
            cancel,
            state,
            join: Some(join),
            result: None,
        })
    }
    pub fn request_stop(&self) -> io::Result<()> {
        self.state.stopping.store(true, Ordering::Release);
        wake(&self.cancel)
    }
    pub fn is_finished(&self) -> bool {
        self.join.as_ref().is_none_or(JoinHandle::is_finished)
    }
    pub fn state(&self) -> &S {
        &self.state.caller
    }
    pub fn join(&mut self, panic_error: E) -> Result<(), E> {
        if let Some(join) = self.join.take() {
            self.result = Some(join.join().unwrap_or(Err(panic_error)));
        }
        self.result.unwrap_or(Ok(()))
    }

    /// Join completed work even if its cancellation source has failed. If a
    /// running worker cannot be woken, retain its handle/resources for retry;
    /// returning that error does not claim quiescence.
    pub fn stop_and_join(&mut self, io_error: E) -> Result<(), E> {
        if !self.is_finished() && self.request_stop().is_err() && !self.is_finished() {
            return Err(io_error);
        }
        self.join(io_error)
    }
}
fn wake(cancel: &impl Wake) -> io::Result<()> {
    loop {
        match cancel.wake() {
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            result => return result,
        }
    }
}
impl<C: Wake, E: Copy, S> Drop for Worker<C, E, S> {
    fn drop(&mut self) {
        self.state.stopping.store(true, Ordering::Release);
        if self.join.as_ref().is_some_and(|join| !join.is_finished()) {
            let _ = wake(&self.cancel);
        }
        // No hidden join. The worker owns device resources until cleanup completes.
    }
}
