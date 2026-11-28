use navigation::observations::Measurement;
use std::sync::mpsc;
use std::thread;
use std::time::Duration;
use telemetry::{
    export::probe::{self, Stage},
    CapturePublisher,
};

pub(crate) fn imu(source: usize, sequence: u64, time: u64) -> Measurement {
    Measurement::Imu(
        source,
        imu::ImuSample {
            sequence,
            timestamp_us: time * 1_000 + 700,
            is_capture_time: true,
            sample_interval_us: 10_000,
            accel_mps2: [-4096.0, 0.0, 4095.875],
            gyro_rps: [-0.25, 1.0, 8191.75],
        },
    )
}

pub(crate) struct PausedRecord {
    resume: Option<mpsc::Sender<()>>,
    worker: Option<thread::JoinHandle<CapturePublisher>>,
}

impl PausedRecord {
    pub(crate) fn release(mut self) -> CapturePublisher {
        self.resume.take().unwrap().send(()).unwrap();
        self.worker.take().unwrap().join().unwrap()
    }
}

impl Drop for PausedRecord {
    fn drop(&mut self) {
        if let Some(resume) = self.resume.take() {
            let _ = resume.send(());
        }
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

pub(crate) fn pause_record(mut publisher: CapturePublisher, value: Measurement) -> PausedRecord {
    let (claimed, observed) = mpsc::channel();
    let (resume, released) = mpsc::channel();
    let worker = thread::spawn(move || {
        probe::record_with_hooks(&mut publisher, value, |stage| {
            if stage == Stage::AfterSlotClaim {
                claimed.send(()).unwrap();
                released.recv().unwrap();
            }
        })
        .unwrap();
        publisher
    });
    let paused = PausedRecord {
        resume: Some(resume),
        worker: Some(worker),
    };
    observed.recv_timeout(Duration::from_secs(2)).unwrap();
    paused
}
