//! Bounded GPIO markers for matching IMU samples to analyzer traces.
#![allow(unsafe_code)]

use std::time::Instant;

const SAMPLES: usize = 32;

#[derive(Default)]
pub(crate) struct Report {
    brackets_ns: [u64; SAMPLES],
    count: usize,
    clock_resolution_ns: u64,
    failed: bool,
}

impl Report {
    fn record(&mut self, sequence: u64, write: impl FnOnce(bool) -> bool) {
        if self.failed || self.count == SAMPLES {
            return;
        }
        if sequence != self.count as u64 + 1 {
            self.failed = true;
            return;
        }
        let high = self.count.is_multiple_of(2);
        let start = Instant::now();
        if !write(high) {
            self.failed = true;
            return;
        }
        let Some(elapsed) = Instant::now().checked_duration_since(start) else {
            self.failed = true;
            return;
        };
        self.brackets_ns[self.count] = u64::try_from(elapsed.as_nanos()).unwrap_or(u64::MAX);
        self.count += 1;
    }

    fn valid(&self) -> bool {
        self.count == SAMPLES && !self.failed && self.clock_resolution_ns != 0
    }

    #[cfg(target_os = "nuttx")]
    pub(crate) fn print(&self) -> bool {
        let valid = self.valid();
        let status = if valid { "pass" } else { "fail" };
        println!(
            "{{\"delivery_marker_schema\":1,\"status\":\"{status}\",\"count\":{},\
             \"clock_resolution_ns\":{},\"bracket_ns\":{:?}}}",
            self.count,
            self.clock_resolution_ns,
            &self.brackets_ns[..self.count],
        );
        valid
    }
}

#[cfg(target_os = "nuttx")]
pub(crate) use nuttx::DeliveryMarker;

#[cfg(target_os = "nuttx")]
mod nuttx {
    use super::Report;
    use messaging::{bounded, Inbox, Sender};
    use std::{fs::File, io, os::fd::AsRawFd};

    unsafe extern "C" {
        fn s8_marker_write(fd: i32, high: i32) -> i32;
        fn s8_marker_clock_resolution_ns() -> i32;
    }

    pub(crate) struct DeliveryMarker {
        output: File,
        report: Report,
        report_sender: Sender<Report>,
    }

    impl DeliveryMarker {
        pub(crate) fn open() -> io::Result<(Self, Inbox<Report>)> {
            // SAFETY: The helper takes no arguments and returns a scalar.
            let resolution_ns = unsafe { s8_marker_clock_resolution_ns() };
            if resolution_ns <= 0 {
                return Err(io::Error::last_os_error());
            }
            let output = File::options().write(true).open("/dev/gpio0")?;
            if !set_level(&output, false) {
                return Err(io::Error::last_os_error());
            }

            let (report_sender, inbox) = bounded(1);
            Ok((
                Self {
                    output,
                    report: Report {
                        clock_resolution_ns: resolution_ns as u64,
                        ..Report::default()
                    },
                    report_sender,
                },
                inbox,
            ))
        }

        pub(crate) fn record(&mut self, sequence: u64) {
            let output = &self.output;
            self.report.record(sequence, |high| set_level(output, high));
        }
    }

    impl Drop for DeliveryMarker {
        fn drop(&mut self) {
            // An invalid or incomplete run may have left the pin high. The
            // successful 32-edge run already ended low.
            if !self.report.valid() {
                set_level(&self.output, false);
            }
            let _ = self
                .report_sender
                .try_send(std::mem::take(&mut self.report));
        }
    }

    fn set_level(output: &File, high: bool) -> bool {
        // SAFETY: The owned descriptor remains open during this scalar ioctl.
        unsafe { s8_marker_write(output.as_raw_fd(), i32::from(high)) == 0 }
    }
}

#[cfg(test)]
mod tests {
    use super::{Report, SAMPLES};

    #[test]
    fn captures_a_fixed_window_with_one_alternating_edge_per_sample() {
        let mut report = Report {
            clock_resolution_ns: 1_000_000,
            ..Report::default()
        };
        for sequence in 1..=SAMPLES as u64 {
            report.record(sequence, |high| {
                assert_eq!(high, sequence % 2 == 1);
                true
            });
        }
        assert!(report.valid());
        report.record(SAMPLES as u64 + 1, |_| panic!("capture must stay bounded"));
        assert_eq!(report.count, SAMPLES);
    }

    #[test]
    fn incomplete_capture_sequence_gap_and_gpio_failure_are_invalid() {
        let mut report = Report {
            clock_resolution_ns: 1_000_000,
            ..Report::default()
        };
        report.record(1, |_| true);
        assert!(!report.valid());
        report.record(3, |_| panic!("gap must not toggle the pin"));
        report.record(2, |_| panic!("capture failure must be sticky"));
        assert_eq!(report.count, 1);
        assert!(report.failed);

        let mut report = Report::default();
        report.record(1, |_| false);
        report.record(1, |_| panic!("GPIO failure must be sticky"));
        assert_eq!(report.count, 0);
        assert!(report.failed);
    }
}
