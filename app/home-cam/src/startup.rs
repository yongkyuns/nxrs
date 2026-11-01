//! Trial confirmation after recent camera/TLS health and joined retirement.
//! The product must join Wi-Fi before acquiring its exclusive quiet owner.
use crate::{LiveCamera, StreamOutput};
use boot::{Control, State};
use camera::Camera;
use camera_service::Frames;
use watchdog::ArmedWatchdog;
use wifi::QuiescenceControl;

#[derive(Debug)]
pub enum Error {
    Boot(boot::DeviceError),
    Watchdog(watchdog::DeviceError),
    Wifi(wifi::Error),
    Stream(crate::Error),
    Unhealthy,
}

/// Only successful, consuming stream retirement creates this receipt.
/// Neither construction nor confirmation asserts physical flash-bus quiescence.
pub struct Health {
    observed_ms: u64,
    expires_ms: u64,
}
impl Health {
    fn valid(&self, now_ms: u64) -> bool {
        self.observed_ms <= now_ms && now_ms <= self.expires_ms
    }
}

/// Retains the inherited watchdog through every uncertain startup failure.
pub struct Trial<B, W> {
    boot: B,
    watchdog: W,
}
impl<B: Control, W: ArmedWatchdog> Trial<B, W> {
    /// Adopt the watchdog before calling this, including on stable boots.
    /// Stable boots explicitly cancel it before interactive provisioning.
    pub fn begin(mut boot: B, mut watchdog: W) -> Result<Option<Self>, Error> {
        match boot.state().map_err(Error::Boot)? {
            State::Stable => {
                watchdog.disarm().map_err(Error::Watchdog)?;
                Ok(None)
            }
            State::Trial => Ok(Some(Self { boot, watchdog })),
            State::Pending => Err(Error::Boot(boot::DeviceError::InvalidState)),
        }
    }

    /// Caller has joined Wi-Fi and acquired its exclusive maintenance owner.
    /// Recheck health after quiescing, immediately before writing confirmation.
    /// A failure after confirmation cannot undo Stable metadata; the watchdog
    /// remains armed until successful resume and explicit cancellation.
    pub fn complete(
        mut self,
        health: Health,
        quiet: &mut impl QuiescenceControl,
        mut now: impl FnMut() -> u64,
    ) -> Result<(), Error> {
        let before = now();
        if !health.valid(before) {
            return Err(Error::Unhealthy);
        }
        quiet.quiesce().map_err(Error::Wifi)?;
        let after = now();
        if after < before || !health.valid(after) {
            return Err(Error::Unhealthy);
        }
        self.boot.confirm().map_err(Error::Boot)?;
        if self.boot.state().map_err(Error::Boot)? != State::Stable {
            return Err(Error::Boot(boot::DeviceError::InvalidState));
        }
        quiet.resume().map_err(Error::Wifi)?;
        self.watchdog.disarm().map_err(Error::Watchdog)
    }
}

fn health<D: Camera, const N: usize, B: AsRef<[u8]> + AsMut<[u8]>, O: StreamOutput>(
    live: &LiveCamera<D, N, B, O>,
    now_ms: u64,
    max_age_ms: u64,
) -> Option<Health> {
    if max_age_ms == 0
        || live.capture.state() != camera_service::CameraState::Running
        || live.server.stopped()
    {
        return None;
    }
    let sequence = live.capture.latest_sequence();
    let capture = live.capture.next_after(sequence.saturating_sub(1))?;
    let stats = live.stats();
    if stats.tls?.handshakes == 0 {
        return None;
    }
    let delivered = stats.stream.latest_completed?;
    if delivered.sequence == 0
        || delivered.sequence > sequence
        || delivered.captured_ms > delivered.completed_ms
        || delivered.completed_ms > now_ms
    {
        return None;
    }
    let mut oldest = capture.capture.timestamp_ms.min(delivered.captured_ms);
    if capture.capture.timestamp_ms > now_ms {
        return None;
    }
    if let Some(detection) = live.detection.as_ref() {
        if detection.stopped() {
            return None;
        }
        let observed = detection.snapshot().latest?;
        if observed.sequence == 0 || observed.sequence > sequence || observed.captured_ms > now_ms {
            return None;
        }
        oldest = oldest.min(observed.captured_ms);
    }
    let receipt = Health {
        observed_ms: now_ms,
        expires_ms: oldest.saturating_add(max_age_ms),
    };
    receipt.valid(now_ms).then_some(receipt)
}

pub fn healthy<D: Camera, const N: usize, B: AsRef<[u8]> + AsMut<[u8]>, O: StreamOutput>(
    live: &LiveCamera<D, N, B, O>,
    now_ms: u64,
    max_age_ms: u64,
) -> bool {
    health(live, now_ms, max_age_ms).is_some()
}

/// Consume the exact pipeline whose health is checked, joining TLS, camera and
/// detection before returning the non-reusable confirmation receipt.
pub fn retire_healthy<D: Camera, const N: usize, B: AsRef<[u8]> + AsMut<[u8]>, O: StreamOutput>(
    mut live: LiveCamera<D, N, B, O>,
    now_ms: u64,
    max_age_ms: u64,
) -> Result<Health, Error> {
    let receipt = health(&live, now_ms, max_age_ms);
    let stopped = live.stop();
    drop(live);
    stopped.map_err(Error::Stream)?;
    receipt.ok_or(Error::Unhealthy)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::RefCell, rc::Rc};

    #[derive(Clone)]
    struct Trace {
        calls: Rc<RefCell<Vec<&'static str>>>,
        fail: &'static str,
    }
    impl Trace {
        fn call(&self, name: &'static str) -> bool {
            self.calls.borrow_mut().push(name);
            self.fail == name
        }
    }
    struct Boot(Trace, State);
    impl Control for Boot {
        fn state(&mut self) -> Result<State, boot::DeviceError> {
            if self.0.call("state") {
                Err(boot::DeviceError::Io)
            } else {
                Ok(self.1)
            }
        }
        fn stage_update(
            &mut self,
            _: &mut dyn std::io::Read,
            _: u32,
        ) -> Result<(), boot::DeviceError> {
            panic!("no staging during confirmation")
        }
        fn request_trial(&mut self) -> Result<(), boot::DeviceError> {
            panic!("no trial requests during confirmation")
        }
        fn confirm(&mut self) -> Result<(), boot::DeviceError> {
            if self.0.call("confirm") {
                return Err(boot::DeviceError::Io);
            }
            if self.0.fail != "readback" {
                self.1 = State::Stable;
            }
            Ok(())
        }
    }
    impl ArmedWatchdog for Trace {
        fn disarm(&mut self) -> Result<(), watchdog::DeviceError> {
            if self.call("disarm") {
                Err(watchdog::DeviceError::Io)
            } else {
                Ok(())
            }
        }
    }
    impl QuiescenceControl for Trace {
        fn quiesce(&mut self) -> Result<(), wifi::Error> {
            if self.call("quiet") {
                Err(wifi::Error::Io)
            } else {
                Ok(())
            }
        }
        fn resume(&mut self) -> Result<(), wifi::Error> {
            if self.call("resume") {
                Err(wifi::Error::Io)
            } else {
                Ok(())
            }
        }
    }
    fn trace(fail: &'static str) -> Trace {
        Trace {
            calls: Rc::default(),
            fail,
        }
    }

    struct Source(Trace);
    impl Camera for Source {
        fn start(&mut self, format: camera::Format) -> Result<camera::Format, camera::DeviceError> {
            Ok(format)
        }
        fn poll_frame(
            &mut self,
            now: u64,
            bytes: &mut [u8],
        ) -> Result<Option<camera::Capture>, camera::DeviceError> {
            bytes[..4].copy_from_slice(&[0xff, 0xd8, 0xff, 0xd9]);
            Ok(Some(camera::Capture {
                format: camera::Format {
                    width: 96,
                    height: 96,
                    pixels: camera::PixelFormat::Jpeg,
                },
                len: 4,
                timestamp_ms: now,
            }))
        }
        fn stop(&mut self) -> Result<(), camera::DeviceError> {
            if self.0.call("camera") {
                Err(camera::DeviceError::Io)
            } else {
                Ok(())
            }
        }
    }
    struct Output(Trace, stream_service::ServerStats, Option<crate::TlsStats>);
    impl StreamOutput for Output {
        fn poll(
            &mut self,
            _: u64,
            _: bool,
            _: Option<camera::Frame<'_>>,
            _: Option<stream_service::Metadata<'_>>,
        ) -> Result<stream_service::Progress, crate::Error> {
            Ok(Default::default())
        }
        fn stop(&mut self) -> Result<(), crate::Error> {
            self.0.call("output");
            Ok(())
        }
        fn stats(&self) -> (stream_service::ServerStats, Option<crate::TlsStats>) {
            (self.1, self.2)
        }
        fn stopped(&self) -> bool {
            false
        }
    }
    fn live(trace: Trace) -> LiveCamera<Source, 4, [u8; 4], Output> {
        LiveCamera {
            capture: camera_service::CameraService::with_buffers(
                Source(trace.clone()),
                [[0; 4]],
                [0; 4],
            )
            .unwrap(),
            server: Output(
                trace,
                stream_service::ServerStats {
                    latest_completed: Some(stream_service::CompletedFrame {
                        sequence: 1,
                        captured_ms: 100,
                        completed_ms: 101,
                    }),
                    ..Default::default()
                },
                Some(crate::TlsStats {
                    handshakes: 1,
                    ..Default::default()
                }),
            ),
            stats: Default::default(),
            detection: None,
        }
    }

    #[test]
    fn health_requires_recent_authenticated_delivery_and_successful_retirement() {
        for fail in [
            "",
            "camera",
            "no-frame",
            "plaintext",
            "no-handshake",
            "future",
            "stale",
        ] {
            let trace = trace(fail);
            let mut live = live(trace.clone());
            live.start(96, 96).unwrap();
            live.poll(100, true).unwrap();
            match fail {
                "no-frame" => live.server.1.latest_completed = None,
                "plaintext" => live.server.2 = None,
                "no-handshake" => live.server.2.as_mut().unwrap().handshakes = 0,
                "future" => {
                    live.server
                        .1
                        .latest_completed
                        .as_mut()
                        .unwrap()
                        .completed_ms = 111
                }
                "stale" => {
                    live.poll(6000, true).unwrap();
                }
                _ => (),
            }
            let now = if fail == "stale" { 6000 } else { 110 };
            assert_eq!(healthy(&live, now, 5000), ["", "camera"].contains(&fail));
            assert_eq!(retire_healthy(live, now, 5000).is_ok(), fail.is_empty());
            assert_eq!(&*trace.calls.borrow(), &["output", "camera"]);
        }
    }

    #[test]
    fn attached_detector_requires_a_fresh_real_frame_result() {
        struct Detector;
        impl detector::Detector for Detector {
            fn capabilities(&self) -> detector::Capabilities {
                detector::Capabilities::presence(96, 96)
            }
            fn detect(
                &mut self,
                _: detector::Image<'_>,
            ) -> Result<detector::DetectionResult, detector::DeviceError> {
                Ok(detector::DetectionResult::presence(detector::Score::new(
                    0,
                )?))
            }
        }
        let worker = detection_service::Worker::spawn(
            detection_service::WorkerOptions {
                frame_capacity: 4,
                sample_interval_ms: 1,
                stack_bytes: 65536,
            },
            || Ok(Detector),
        )
        .unwrap();
        let mut live = live(trace(""));
        live.attach_detection(
            crate::SecurityDetection::new(worker, detection_service::Policy::default()).unwrap(),
        )
        .unwrap();
        live.start(96, 96).unwrap();
        live.poll(100, true).unwrap();
        assert!(!healthy(&live, 110, 5000));
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
        while !healthy(&live, 110, 5000) {
            assert!(std::time::Instant::now() < deadline);
            live.poll(110, true).unwrap();
            std::thread::yield_now();
        }
        assert!(live.detection().unwrap().snapshot().worker.completed > 0);
        live.detection.as_mut().unwrap().stop().unwrap();
        assert!(!healthy(&live, 110, 5000));
        assert!(retire_healthy(live, 110, 5000).is_err());
    }

    #[test]
    fn begin_disarms_only_stable_and_retains_trial_deadline() {
        for (state, fail, expected) in [
            (State::Stable, "", &["state", "disarm"][..]),
            (State::Trial, "", &["state"][..]),
            (State::Pending, "", &["state"][..]),
            (State::Stable, "state", &["state"][..]),
            (State::Stable, "disarm", &["state", "disarm"][..]),
        ] {
            let trace = trace(fail);
            let result = Trial::begin(Boot(trace.clone(), state), trace.clone());
            assert_eq!(result.is_err(), state == State::Pending || !fail.is_empty());
            if let Ok(owner) = result {
                assert_eq!(owner.is_some(), state == State::Trial);
            }
            assert_eq!(&*trace.calls.borrow(), expected);
        }
    }

    #[test]
    fn confirmation_order_and_each_failure_keep_cancellation_last() {
        for (fail, expected) in [
            ("", &["quiet", "confirm", "state", "resume", "disarm"][..]),
            ("quiet", &["quiet"][..]),
            ("confirm", &["quiet", "confirm"][..]),
            ("state", &["quiet", "confirm", "state"][..]),
            ("readback", &["quiet", "confirm", "state"][..]),
            ("resume", &["quiet", "confirm", "state", "resume"][..]),
            (
                "disarm",
                &["quiet", "confirm", "state", "resume", "disarm"][..],
            ),
        ] {
            let trace = trace(fail);
            let trial = Trial {
                boot: Boot(trace.clone(), State::Trial),
                watchdog: trace.clone(),
            };
            let health = Health {
                observed_ms: 100,
                expires_ms: 200,
            };
            let result = trial.complete(health, &mut trace.clone(), || 150);
            assert_eq!(result.is_ok(), fail.is_empty(), "{fail}");
            assert_eq!(&*trace.calls.borrow(), expected, "{fail}");
        }
        for times in [[99, 100], [201, 201], [150, 201], [150, 149]] {
            let trace = trace("");
            let trial = Trial {
                boot: Boot(trace.clone(), State::Trial),
                watchdog: trace.clone(),
            };
            let health = Health {
                observed_ms: 100,
                expires_ms: 200,
            };
            let mut times = times.into_iter();
            assert!(matches!(
                trial.complete(health, &mut trace.clone(), || times.next().unwrap()),
                Err(Error::Unhealthy)
            ));
            assert!(!trace
                .calls
                .borrow()
                .iter()
                .any(|op| ["confirm", "disarm"].contains(op)));
        }
    }
}
