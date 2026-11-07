//! Bounded scheduler and I²C trace for single-core qualification builds.
#![allow(unsafe_code)]

use std::{
    fs::File,
    io::{self, Read, Write},
    os::fd::AsRawFd,
    time::{Duration, Instant},
};

const WINDOW: Duration = Duration::from_millis(200);
const MAX_TEXT_BYTES: u64 = 256 * 1024;

unsafe extern "C" {
    fn imu_notes_gpio_irq() -> i32;
    fn imu_notes_i2c() -> i32;
    fn imu_notes_enable(fd: i32, enabled: i32) -> i32;
    fn imu_notes_clear(fd: i32) -> i32;
    fn imu_notes_mode(fd: i32) -> i32;
}

enum Window {
    Armed,
    Running(Instant),
    Complete,
}

pub(crate) struct Notes {
    control: File,
    ram: File,
    irq: Option<i32>,
    i2c: bool,
    window: Window,
}

impl Notes {
    pub(crate) fn open() -> io::Result<Option<Self>> {
        // SAFETY: These bindings return target configuration constants.
        let (irq, i2c) = unsafe { (imu_notes_gpio_irq(), imu_notes_i2c() != 0) };
        if irq < 0 && !i2c {
            return Ok(None);
        }
        let notes = Self {
            control: File::open("/dev/notectl")?,
            ram: File::open("/dev/note/ram")?,
            irq: (irq >= 0).then_some(irq),
            i2c,
            window: Window::Armed,
        };
        notes.enable(false)?;
        Ok(Some(notes))
    }

    pub(crate) fn start(&mut self) -> io::Result<()> {
        if matches!(self.window, Window::Armed) {
            // SAFETY: The owned descriptor remains open for this ioctl.
            check(unsafe { imu_notes_clear(self.ram.as_raw_fd()) })?;
            if self.mode()? != 0 {
                return Err(io::Error::other("scheduler trace must disable overwrite"));
            }
            self.enable(true)?;
            self.window = Window::Running(Instant::now() + WINDOW);
        }
        Ok(())
    }

    pub(crate) fn deadline(&self) -> Option<Instant> {
        match self.window {
            Window::Running(deadline) => Some(deadline),
            _ => None,
        }
    }

    pub(crate) fn stop_if_due(&mut self, now: Instant) -> io::Result<()> {
        if self.deadline().is_some_and(|deadline| now >= deadline) {
            self.enable(false)?;
            self.window = Window::Complete;
        }
        Ok(())
    }

    pub(crate) fn finish(mut self) -> io::Result<bool> {
        let window_complete = matches!(self.window, Window::Complete);
        if matches!(self.window, Window::Running(_)) {
            self.enable(false)?;
            self.window = Window::Armed;
        }

        let mode = self.mode()?;
        let irq = self
            .irq
            .map_or_else(|| "none".into(), |irq| irq.to_string());
        let i2c = u8::from(self.i2c);
        let lost = u8::from(mode == 2);
        let mut out = io::stdout().lock();
        writeln!(
            out,
            "NXRS_NOTE_BEGIN schema=2 source=nuttx-noteram-ascii \
             clock=perf_gettime-converted cpus=1 irq={irq} worker=imu-acquire \
             mode={mode} notes_lost={lost} window_ms={} i2c={i2c}",
            WINDOW.as_millis(),
        )?;
        let bytes = io::copy(&mut (&mut self.ram).take(MAX_TEXT_BYTES), &mut out)?;
        let complete = window_complete && bytes != 0 && bytes < MAX_TEXT_BYTES;
        writeln!(
            out,
            "NXRS_NOTE_END schema=2 mode={mode} notes_lost={lost} complete={}",
            u8::from(complete)
        )?;
        Ok(complete && lost == 0)
    }

    fn enable(&self, enabled: bool) -> io::Result<()> {
        // SAFETY: The owned descriptor remains open, and the binding passes a
        // target-header value to a synchronous ioctl.
        check(unsafe { imu_notes_enable(self.control.as_raw_fd(), i32::from(enabled)) })
    }

    fn mode(&self) -> io::Result<i32> {
        // SAFETY: The owned descriptor remains open for this ioctl.
        match unsafe { imu_notes_mode(self.ram.as_raw_fd()) } {
            mode @ (0 | 2) => Ok(mode),
            -1 => Err(io::Error::last_os_error()),
            _ => Err(io::Error::other(
                "unexpected scheduler trace overwrite mode",
            )),
        }
    }
}

impl Drop for Notes {
    fn drop(&mut self) {
        if matches!(self.window, Window::Running(_)) {
            let _ = self.enable(false);
        }
    }
}

fn check(result: i32) -> io::Result<()> {
    if result == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}
