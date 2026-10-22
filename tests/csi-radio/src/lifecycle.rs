//! Test-image lifecycle. SDK errors stay typed; all cleanup steps are attempted.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SdkError(pub i32);

pub trait Sdk {
    fn configure(&mut self) -> Result<(), SdkError>;
    fn register(&mut self, epoch: u32) -> Result<(), SdkError>;
    fn enable(&mut self, enabled: bool) -> Result<(), SdkError>;
    fn force(&mut self, agc: u8, fft: i8) -> Result<(), SdkError>;
}

pub struct Lifecycle<R> {
    radio: R,
    retired: bool,
    forced: bool,
    failed: Option<SdkError>,
}
impl<R: Sdk> Lifecycle<R> {
    pub fn new(radio: R) -> Self {
        Self {
            radio,
            retired: true,
            forced: false,
            failed: None,
        }
    }
    pub fn start(&mut self, epoch: u32) -> Result<(), SdkError> {
        if let Some(error) = self.failed {
            return Err(error);
        }
        if epoch == 0 || !self.retired {
            return Err(SdkError(-1));
        }
        self.retired = false;
        let result = self
            .radio
            .configure()
            .and_then(|()| self.radio.register(epoch))
            .and_then(|()| self.radio.enable(true));
        if let Err(error) = result {
            let _ = self.stop(); // Preserve the startup error even if cleanup fails.
            return Err(error);
        }
        Ok(())
    }
    pub fn force(&mut self, agc: u8, fft: i8) -> Result<(), SdkError> {
        if self.retired {
            return Err(SdkError(-1));
        }
        self.forced = true; // A failed native call may have changed RF state.
        self.radio.force(agc, fft)
    }
    pub fn stop(&mut self) -> Result<(), SdkError> {
        if !self.retired {
            self.retired = true;
            let gain = if self.forced {
                self.radio.force(0, 0)
            } else {
                Ok(())
            };
            let disable = self.radio.enable(false);
            let unregister = self.radio.register(0);
            self.forced = false;
            self.failed = self
                .failed
                .or_else(|| gain.err())
                .or_else(|| disable.err())
                .or_else(|| unregister.err());
        }
        self.failed.map_or(Ok(()), Err)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[derive(Default)]
    struct Fake {
        calls: Vec<String>,
        fail: Vec<&'static str>,
    }
    impl Fake {
        fn call(
            &mut self,
            stage: &'static str,
            arg: impl std::fmt::Display,
        ) -> Result<(), SdkError> {
            self.calls.push(format!("{stage}:{arg}"));
            if self.fail.contains(&stage) {
                Err(SdkError(17))
            } else {
                Ok(())
            }
        }
    }
    impl Sdk for Fake {
        fn configure(&mut self) -> Result<(), SdkError> {
            self.call("configure", 0)
        }
        fn register(&mut self, epoch: u32) -> Result<(), SdkError> {
            self.call(if epoch == 0 { "unregister" } else { "register" }, epoch)
        }
        fn enable(&mut self, on: bool) -> Result<(), SdkError> {
            self.call(if on { "enable" } else { "disable" }, on)
        }
        fn force(&mut self, agc: u8, _: i8) -> Result<(), SdkError> {
            self.call(if agc == 0 { "restore" } else { "force" }, agc)
        }
    }
    #[test]
    fn passive_capture_does_not_touch_phy_gain() {
        let mut s = Lifecycle::new(Fake::default());
        s.start(1).unwrap();
        s.stop().unwrap();
        s.stop().unwrap();
        assert_eq!(
            s.radio.calls,
            [
                "configure:0",
                "register:1",
                "enable:true",
                "disable:false",
                "unregister:0"
            ]
        );
    }
    #[test]
    fn every_start_failure_rolls_back_and_stays_failed() {
        for stage in ["configure", "register", "enable"] {
            let mut s = Lifecycle::new(Fake {
                fail: vec![stage],
                ..Fake::default()
            });
            assert_eq!(s.start(1), Err(SdkError(17)));
            assert_eq!(
                &s.radio.calls[s.radio.calls.len() - 2..],
                ["disable:false", "unregister:0"]
            );
        }
    }
    #[test]
    fn failed_force_still_restores_auto_gain() {
        let mut s = Lifecycle::new(Fake {
            fail: vec!["force"],
            ..Fake::default()
        });
        s.start(1).unwrap();
        assert!(s.force(20, -3).is_err());
        s.stop().unwrap();
        assert_eq!(
            &s.radio.calls[s.radio.calls.len() - 3..],
            ["restore:0", "disable:false", "unregister:0"]
        );
    }
    #[test]
    fn cleanup_attempts_all_steps_and_failure_is_sticky() {
        let mut s = Lifecycle::new(Fake {
            fail: vec!["restore", "disable", "unregister"],
            ..Fake::default()
        });
        s.start(1).unwrap();
        s.force(20, -3).unwrap();
        assert!(s.stop().is_err());
        assert!(s.stop().is_err());
        assert!(s.start(2).is_err());
        assert_eq!(
            &s.radio.calls[s.radio.calls.len() - 3..],
            ["restore:0", "disable:false", "unregister:0"]
        );
    }
    #[test]
    fn overlapping_start_and_force_after_retirement_are_rejected() {
        let mut s = Lifecycle::new(Fake::default());
        assert!(s.force(20, 4).is_err());
        s.start(1).unwrap();
        assert!(s.start(2).is_err());
        s.stop().unwrap();
        assert!(s.force(20, 4).is_err());
    }
}
