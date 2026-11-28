//! GNSS delivery policy and session lifetime.
use crate::{DeviceError, Event, Session};
use acquisition::{Wake, Worker};
use messaging::{DeliveryError, DeliveryResult};
use std::{
    sync::atomic::{AtomicBool, AtomicU32, Ordering},
    thread,
    time::Duration,
};

type SessionWorker<C> = Worker<C, DeviceError, AtomicU32>;

pub(crate) fn start<C: Wake>(
    stack_bytes: usize,
    cancel: C,
    delivery: (
        impl FnMut(Event) -> DeliveryResult<Event> + Send + 'static,
        impl Send + 'static,
    ),
    run: impl FnOnce(&AtomicBool, &mut dyn FnMut(Event) -> bool) -> Result<(), DeviceError>
        + Send
        + 'static,
) -> std::io::Result<SessionWorker<C>> {
    Worker::start(
        "gnss-acquire",
        stack_bytes,
        cancel,
        AtomicU32::new(0),
        move |stop, dropped| {
            // Capture the tuple so the sink is destroyed before the retained
            // device lease, including spawn failure and unwinding.
            let mut delivery = delivery;
            let sink = &mut delivery.0;
            let mut emit = |event| match sink(event) {
                Ok(()) => true,
                Err(rejected) if rejected.error == DeliveryError::Full => {
                    let _ = dropped.try_update(Ordering::Relaxed, Ordering::Relaxed, |count| {
                        Some(count.saturating_add(1))
                    });
                    true
                }
                Err(_) => false,
            };
            let result = run(stop, &mut emit);
            if let Err(error) = result {
                // Keep a single terminal failure until admitted, closed or
                // canceled. The join result independently retains the error.
                let mut pending = Event::Failed(error);
                while !stop.load(Ordering::Acquire) {
                    match sink(pending) {
                        Ok(()) => break,
                        Err(rejected) if rejected.error == DeliveryError::Full => {
                            pending = rejected.value;
                            thread::park_timeout(Duration::from_millis(5));
                        }
                        Err(_) => break,
                    }
                }
            }
            result
        },
    )
}

impl<C: Wake> Session for SessionWorker<C> {
    fn request_stop(&mut self) -> Result<(), DeviceError> {
        Worker::request_stop(self).map_err(|_| DeviceError::Io)
    }

    fn join(&mut self) -> Result<(), DeviceError> {
        self.stop_and_join(DeviceError::Io)
    }

    fn is_finished(&self) -> bool {
        Worker::is_finished(self)
    }

    fn dropped(&self) -> u32 {
        self.state().load(Ordering::Relaxed)
    }
}
