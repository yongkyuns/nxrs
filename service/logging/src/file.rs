use crate::{archive, install, Line};
use log::LevelFilter;
use std::io::{self, Read};
use std::num::NonZeroU64;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender, TrySendError};
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

struct Records<Record> {
    receiver: Receiver<Record>,
    pending: Option<Record>,
    offset: usize,
}

impl<Record: AsRef<[u8]>> Read for Records<Record> {
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
        let mut bytes = &line.as_ref()[self.offset..];
        let count = bytes.read(output)?;
        self.offset += count;
        if bytes.is_empty() {
            self.pending = None;
        }
        Ok(count)
    }
}

fn spawn_archive<Record: AsRef<[u8]> + Send + 'static>(
    path: PathBuf,
    limit: NonZeroU64,
    capacity: usize,
) -> io::Result<(SyncSender<Record>, JoinHandle<io::Result<()>>)> {
    let (sender, receiver) = sync_channel(capacity);
    let worker = thread::Builder::new()
        .name("rotating-archive".into())
        .spawn(move || {
            let Ok(first) = receiver.recv() else {
                return Ok(());
            };
            archive(
                Records {
                    receiver,
                    pending: Some(first),
                    offset: 0,
                },
                &path,
                limit,
            )
        })?;
    Ok((sender, worker))
}

/// Install rotating file output on a desktop host. Log callers only attempt
/// bounded admission. The writer opens the archive when its first record arrives.
pub fn start_file(path: PathBuf, limit: NonZeroU64, level: LevelFilter) -> io::Result<FileLog> {
    let (sender, worker) = spawn_archive(path, limit, QUEUE_CAPACITY)?;
    let admission = Arc::new(Admission {
        sender: Mutex::new(Some(sender)),
        dropped: AtomicUsize::new(0),
    });
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

/// A single producer writes complete encoded records to the archive worker's
/// bounded inbox. Each path belongs to one archive writer.
pub struct FileArchive<Record> {
    sender: Option<SyncSender<Record>>,
    worker: Option<JoinHandle<io::Result<()>>>,
    dropped: usize,
}

impl<Record: AsRef<[u8]> + Send + 'static> FileArchive<Record> {
    pub fn start(path: PathBuf, limit: NonZeroU64, capacity: usize) -> io::Result<Self> {
        if capacity == 0 {
            return Err(io::ErrorKind::InvalidInput.into());
        }
        let (sender, worker) = spawn_archive(path, limit, capacity)?;
        Ok(Self {
            sender: Some(sender),
            worker: Some(worker),
            dropped: 0,
        })
    }

    /// Reject the newest record if the inbox is full.
    pub fn offer(&mut self, record: Record) -> io::Result<()> {
        let sender = self.sender.as_ref().ok_or(io::ErrorKind::BrokenPipe)?;
        match sender.try_send(record) {
            Ok(()) => {}
            Err(TrySendError::Full(_)) => self.dropped = self.dropped.saturating_add(1),
            Err(TrySendError::Disconnected(_)) => {
                self.close()?;
                return Err(io::ErrorKind::BrokenPipe.into());
            }
        }
        Ok(())
    }

    pub fn finish(mut self) -> io::Result<usize> {
        self.close()?;
        Ok(self.dropped)
    }
}

impl<Record> FileArchive<Record> {
    fn close(&mut self) -> io::Result<()> {
        self.sender.take();
        match self.worker.take() {
            Some(worker) => worker
                .join()
                .map_err(|_| io::Error::other("archive task panicked"))?,
            None => Ok(()),
        }
    }
}

impl<Record> Drop for FileArchive<Record> {
    fn drop(&mut self) {
        let _ = self.close();
    }
}
