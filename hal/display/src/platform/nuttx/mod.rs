use crate::{Display, Error, Rect, Size};
use ili9342c::{Interface, Panel};
use std::{
    fs::File,
    os::fd::AsFd,
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

const FREQUENCY_HZ: u32 = 20_000_000;

struct Spi(File);

impl Interface for Spi {
    type Error = Error;

    fn write(&mut self, command: u8, data: &[u8]) -> Result<(), Error> {
        ffi::write(self.0.as_fd(), FREQUENCY_HZ, command, data)
    }
}

struct Screen {
    panel: Panel<Spi>,
    _lease: Lease,
}

/// Open and initialize the configured display.
pub fn open() -> Result<impl Display, Error> {
    if TAKEN.swap(true, Ordering::AcqRel) {
        return Err(Error::Busy);
    }
    let lease = Lease;
    let file = File::open(env!("DISPLAY_DEVICE")).map_err(|_| Error::Io)?;
    let panel = Panel::initialize(Spi(file))?;
    Ok(Screen {
        panel,
        _lease: lease,
    })
}

impl Display for Screen {
    fn size(&self) -> Size {
        Size {
            width: ili9342c::WIDTH,
            height: ili9342c::HEIGHT,
        }
    }

    fn draw(&mut self, area: Rect, pixels: &[u16]) -> Result<(), Error> {
        self.panel
            .draw(area.x, area.y, area.size.width, area.size.height, pixels)
            .map_err(|error| match error {
                ili9342c::Error::InvalidRegion => Error::InvalidRegion,
                ili9342c::Error::InvalidPixelCount => Error::InvalidPixelCount,
                ili9342c::Error::Transport(error) => error,
            })
    }
}
