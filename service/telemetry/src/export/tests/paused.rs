use super::*;

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
    let epoch = publisher.publisher.as_ref().unwrap().captured_epoch();
    let worker = thread::spawn(move || {
        publisher
            .publisher
            .as_mut()
            .unwrap()
            .offer_with_hooks(epoch, Sample::from(value).record(), |stage| {
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
