//! One persistent numerical helper. Every dispatch joins its borrowed work.
#![forbid(unsafe_code)]
use super::Error;
use std::{
    panic::{catch_unwind, AssertUnwindSafe},
    sync::mpsc::{sync_channel, Receiver, SyncSender},
    thread::{self, JoinHandle},
};

pub(crate) struct Worker<T> {
    jobs: Option<SyncSender<T>>,
    done: Receiver<Result<(), Error>>,
    thread: Option<JoinHandle<()>>,
    execute: fn(T) -> Result<(), Error>,
    failed: bool,
}
impl<T: Send + 'static> Worker<T> {
    pub(crate) fn start(
        stack: usize,
        initialize: impl FnOnce() -> Result<(), Error> + Send + 'static,
        execute: fn(T) -> Result<(), Error>,
    ) -> Result<Self, Error> {
        let (jobs, receive) = sync_channel(1);
        let (finished, done) = sync_channel(1);
        let thread = thread::Builder::new()
            .name("detector-compute".into())
            .stack_size(stack)
            .spawn(move || {
                let ready = guarded(initialize);
                if finished.send(ready).is_err() || ready.is_err() {
                    return;
                }
                while let Ok(work) = receive.recv() {
                    // Ack only after execution and its context guards have retired.
                    let result = guarded(|| execute(work));
                    if finished.send(result).is_err() {
                        break;
                    }
                }
            })
            .map_err(|_| Error::Model)?;
        let mut worker = Self {
            jobs: Some(jobs),
            done,
            thread: Some(thread),
            execute,
            failed: false,
        };
        if worker.done.recv().unwrap_or(Err(Error::Model)).is_err() {
            worker.failed = true;
            return Err(Error::Model); // Drop closes admission and joins startup.
        }
        Ok(worker)
    }
    pub(crate) fn run(&mut self, first: T, second: T) -> Result<(), Error> {
        if self.failed
            || self
                .jobs
                .as_ref()
                .ok_or(Error::Model)?
                .send(second)
                .is_err()
        {
            self.failed = true;
            return Err(Error::Model);
        }
        let first = guarded(|| (self.execute)(first));
        // Never leave SDK argument/tensor storage while the helper can use it.
        // A disconnect means the helper exited; join confirms quiescence.
        let second = self.done.recv().unwrap_or_else(|_| {
            if let Some(thread) = self.thread.take() {
                let _ = thread.join();
            }
            Err(Error::Model)
        });
        let result = first.and(second);
        self.failed = result.is_err();
        result
    }
}
impl<T> Drop for Worker<T> {
    fn drop(&mut self) {
        drop(self.jobs.take());
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}
fn guarded(operation: impl FnOnce() -> Result<(), Error>) -> Result<(), Error> {
    catch_unwind(AssertUnwindSafe(operation)).unwrap_or(Err(Error::Model))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::boxed::Box;
    type Job = Box<dyn FnOnce() -> Result<(), Error> + Send>;
    fn execute(job: Job) -> Result<(), Error> {
        job()
    }
    #[test]
    fn caller_failure_still_joins_borrowed_work_and_failure_is_sticky() {
        let mut worker = Worker::start(32768, || Ok(()), execute).unwrap();
        let (entered, running) = sync_channel(1);
        let (release, gate) = sync_channel(1);
        let thread = thread::spawn(move || {
            let result = worker.run(
                Box::new(|| Err(Error::Model)),
                Box::new(move || {
                    entered.send(()).unwrap();
                    gate.recv().unwrap();
                    Ok(())
                }),
            );
            assert_eq!(result, Err(Error::Model));
            assert_eq!(
                worker.run(Box::new(|| panic!("sticky")), Box::new(|| panic!("sticky"))),
                Err(Error::Model)
            );
        });
        running.recv().unwrap();
        assert!(!thread.is_finished());
        release.send(()).unwrap();
        thread.join().unwrap();
    }
    #[test]
    fn startup_failure_and_helper_unwind_close_cleanly() {
        assert!(Worker::start(32768, || Err(Error::Model), execute).is_err());
        let mut worker = Worker::start(32768, || Ok(()), execute).unwrap();
        assert_eq!(
            worker.run(Box::new(|| Ok(())), Box::new(|| panic!("helper"))),
            Err(Error::Model)
        );
    }
}
