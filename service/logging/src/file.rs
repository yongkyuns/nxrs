use crate::{archive, install, Line};
use log::LevelFilter;
use std::io::{self, Read};
use std::num::NonZeroU64;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};

const QUEUE_CAPACITY: usize = 32;

#[derive(Default)]
struct Admission {
    sender: Mutex<Option<SyncSender<Line>>>,
    dropped: AtomicUsize,
}

impl Admission {
    fn submit(&self, line: Line) {
        if let Ok(sender) = self.sender.try_lock() {
            if sender
                .as_ref()
                .is_none_or(|sender| sender.try_send(line).is_ok())
            {
                return;
            }
        }
        self.dropped.fetch_add(1, Ordering::Relaxed);
    }

    fn close(&self) {
        self.sender
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take();
    }
}

/// Lifetime of a background file writer. Call `finish` to drain and join it.
pub struct FileLog {
    admission: Arc<Admission>,
    worker: Option<JoinHandle<io::Result<()>>>,
}

impl FileLog {
    pub fn finish(mut self) -> io::Result<usize> {
        self.admission.close();
        self.worker
            .take()
            .unwrap()
            .join()
            .map_err(|_| io::Error::other("diagnostic archive task panicked"))??;
        Ok(self.admission.dropped.load(Ordering::Relaxed))
    }
}

impl Drop for FileLog {
    fn drop(&mut self) {
        self.admission.close();
    }
}

struct Lines {
    receiver: Receiver<Line>,
    pending: Option<Line>,
    offset: usize,
}

impl Read for Lines {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        if output.is_empty() {
            return Ok(0);
        }
        if self.pending.is_none() {
            self.pending = self.receiver.recv().ok();
            self.offset = 0;
        }
        let Some(line) = &self.pending else {
            return Ok(0);
        };
        let mut bytes = &line.as_bytes()[self.offset..];
        let count = bytes.read(output)?;
        self.offset += count;
        if bytes.is_empty() {
            self.pending = None;
        }
        Ok(count)
    }
}

/// Install rotating file output on a desktop host. Log callers only attempt
/// bounded admission. The writer opens the archive when its first record arrives.
pub fn start_file(path: PathBuf, limit: NonZeroU64, level: LevelFilter) -> io::Result<FileLog> {
    let (sender, receiver) = sync_channel(QUEUE_CAPACITY);
    let admission = Arc::new(Admission {
        sender: Mutex::new(Some(sender)),
        ..Admission::default()
    });
    let worker = thread::Builder::new()
        .name("diagnostic-archive".into())
        .spawn(move || {
            let Ok(first) = receiver.recv() else {
                return Ok(());
            };
            archive(
                Lines {
                    receiver,
                    pending: Some(first),
                    offset: 0,
                },
                &path,
                limit,
            )
        })?;
    let backend = admission.clone();
    if install(level, move |_, line| backend.submit(line)).is_err() {
        admission.close();
        let _ = worker.join();
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "a logger is already installed",
        ));
    }
    Ok(FileLog {
        admission,
        worker: Some(worker),
    })
}
