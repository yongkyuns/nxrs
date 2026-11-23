//! Publish typed observations to the configured motion telemetry outputs.
mod links;
mod radio;
mod uplink;
mod wire;

use links::{Communications, Pairing};
use logging::FileArchive;
use navigation::observations::{ObservationService, Reading};
use std::io::{self, Write};
use std::num::NonZeroU64;
use std::path::PathBuf;
use std::time::{Duration, Instant};
use wire::Frame;

pub use links::View;
pub use uplink::{Collector, Config as Cellular};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LinkStatus {
    Idle,
    Pairing,
    Paired,
    Connected,
    Unavailable,
}

#[derive(Clone, Copy)]
pub struct Snapshot {
    pub imu: [Option<Reading<imu::ImuSample>>; 2],
    pub gnss: Reading<gnss::GnssFix>,
}

pub fn reading_status<T>(reading: &Reading<T>) -> &'static str {
    match reading {
        Reading::Waiting => "waiting",
        Reading::Live(_) => "live",
        Reading::Stale => "stale",
        Reading::Fault => "fault",
    }
}

pub struct Outputs {
    pub usb: bool,
    pub archive: Option<(PathBuf, NonZeroU64)>,
    pub pair_on_start: bool,
    pub cellular: Option<Cellular>,
}

/// The caller drives the reporting deadline from its existing wait point.
/// TLS delivery retains its own worker. A file worker exists only while archiving.
pub struct Reports {
    interval: Duration,
    next: Instant,
    secondary: bool,
    usb: Option<io::Stdout>,
    archive: Option<FileArchive<Frame>>,
    links: Option<Communications>,
}

impl Reports {
    pub fn start(
        interval: Duration,
        secondary: bool,
        outputs: Outputs,
        notify: impl FnMut() + Send + 'static,
    ) -> io::Result<Self> {
        let links = (outputs.pair_on_start || outputs.cellular.is_some())
            .then(|| Communications::start(outputs.pair_on_start, outputs.cellular, notify))
            .transpose()?;
        let archive = outputs
            .archive
            .map(|(path, limit)| FileArchive::start(path, limit, 4))
            .transpose()?;
        Ok(Self {
            interval,
            next: Instant::now(),
            secondary,
            usb: outputs.usb.then(io::stdout),
            archive,
            links,
        })
    }

    pub fn deadline(&self) -> Instant {
        self.next
    }

    pub fn snapshot(&self, source: &ObservationService<2>, now: Instant) -> io::Result<Snapshot> {
        let snapshot = source.snapshot(now);
        let [primary, secondary] = snapshot.imu;
        let gnss = snapshot.gnss.ok_or(io::ErrorKind::InvalidInput)?;
        Ok(Snapshot {
            imu: [Some(primary), self.secondary.then_some(secondary)],
            gnss,
        })
    }

    /// Advance one reporting deadline. USB and the archive share one encoding;
    /// the stream publishers encode after admitting a snapshot for transmission.
    pub fn publish(
        &mut self,
        source: &ObservationService<2>,
        now: Instant,
    ) -> io::Result<Snapshot> {
        let snapshot = self.snapshot(source, now)?;
        if self.usb.is_some() || self.archive.is_some() {
            let frame = wire::frame(&snapshot)?;
            if let Some(usb) = &self.usb {
                let mut output = usb.lock();
                output.write_all(frame.as_ref())?;
                output.flush()?;
            }
            if let Some(archive) = &mut self.archive {
                archive.offer(frame)?;
            }
        }
        if let Some(links) = &self.links {
            links.offer(snapshot);
        }
        self.next = now + self.interval;
        Ok(snapshot)
    }

    pub fn view(&self, previous: View) -> View {
        self.links
            .as_ref()
            .map_or(previous, |links| links.view(previous))
    }

    pub fn pair(&mut self, notify: impl FnMut() + Send + 'static) -> io::Result<()> {
        if let Some(links) = &self.links {
            links.pair(Pairing::Start);
        } else {
            self.links = Some(Communications::start(true, None, notify)?);
        }
        Ok(())
    }

    pub fn cancel_pairing(&self) {
        if let Some(links) = &self.links {
            links.pair(Pairing::Cancel);
        }
    }

    pub fn finish(&mut self) -> io::Result<()> {
        let links = self.links.take().map(|mut links| links.stop()).transpose();
        let archive = self.archive.take().map(FileArchive::finish).transpose();
        if let Ok(Some(dropped)) = archive {
            if dropped != 0 {
                eprintln!("motion telemetry archive dropped {dropped} reports");
            }
        }
        archive.and(links).map(|_| ())
    }
}
