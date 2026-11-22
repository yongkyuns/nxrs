use crate::{Error, Event, Point, Session};
use acquisition::{nuttx, readiness, Worker};
use std::{
    fs::OpenOptions,
    io::{self, Read},
    os::unix::fs::OpenOptionsExt,
    sync::atomic::{AtomicBool, Ordering},
};

#[allow(unsafe_code)]
mod ffi;

static TAKEN: AtomicBool = AtomicBool::new(false);

struct Lease;
impl Drop for Lease {
    fn drop(&mut self) {
        TAKEN.store(false, Ordering::Release);
    }
}

struct Touch(Worker<readiness::Cancel, Error>);

impl Session for Touch {
    fn is_finished(&self) -> bool {
        self.0.is_finished()
    }

    fn request_stop(&mut self) -> Result<(), Error> {
        self.0.request_stop().map_err(|_| Error::Io)
    }

    fn join(&mut self) -> Result<(), Error> {
        self.0.stop_and_join(Error::Io)
    }
}

pub fn open(mut sink: impl FnMut(Event) -> bool + Send + 'static) -> Result<impl Session, Error> {
    if TAKEN.swap(true, Ordering::AcqRel) {
        return Err(Error::Busy);
    }
    let lease = Lease;
    let layout = ffi::layout();
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(layout.nonblock)
        .open(env!("TOUCH_DEVICE"))
        .map_err(|_| Error::Io)?;
    let cancel = readiness::Cancel::new(nuttx::pipe).map_err(|_| Error::Io)?;
    let wake = cancel.reader.clone();
    Worker::start("touch", 4096, cancel, (), move |stop, _| {
        let _lease = lease;
        let mut file = file;
        let mut bytes = [0; 64];
        let mut finger = None;
        let frame = &mut bytes[..layout.size.into()];
        while !stop.load(Ordering::Acquire) {
            if !readiness::wait(&file, &wake, nuttx::wait).map_err(|_| Error::Io)? {
                break;
            }
            match file.read(frame) {
                Ok(n) if n == frame.len() => {
                    if let Some(event) = report(frame, &layout, &mut finger)? {
                        if !sink(event) {
                            break;
                        }
                    }
                }
                Ok(_) => return Err(Error::InvalidReport),
                Err(e)
                    if matches!(
                        e.kind(),
                        io::ErrorKind::Interrupted | io::ErrorKind::WouldBlock
                    ) => {}
                Err(_) => return Err(Error::Io),
            }
        }
        Ok(())
    })
    .map(Touch)
    .map_err(|_| Error::Io)
}

fn report(
    frame: &[u8],
    layout: &ffi::Layout,
    finger: &mut Option<u8>,
) -> Result<Option<Event>, Error> {
    if frame[..4] != 1_i32.to_ne_bytes() {
        return Err(Error::InvalidReport);
    }
    let point = &frame[usize::from(layout.point)..];
    let id = point[usize::from(layout.id)];
    let flags = point[usize::from(layout.flags)];
    if flags & layout.up != 0 {
        return Ok(finger.take().map(|_| Event::Released));
    }
    if flags & layout.position == 0 || flags & (layout.down | layout.movement) == 0 {
        return Ok(None);
    }
    let coordinate = |offset: u8| {
        let offset = usize::from(offset);
        u16::try_from(i16::from_ne_bytes([point[offset], point[offset + 1]]))
            .map_err(|_| Error::InvalidReport)
    };
    let position = Point {
        x: coordinate(layout.x)?,
        y: coordinate(layout.y)?,
    };
    // A sampled controller can first report a contact while it is moving.
    let previous = finger.replace(id);
    Ok(Some(if previous != Some(id) || flags & layout.down != 0 {
        Event::Pressed(position)
    } else {
        Event::Moved(position)
    }))
}
