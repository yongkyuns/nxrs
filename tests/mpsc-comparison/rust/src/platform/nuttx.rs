pub use super::native::*;
pub const NAME: &str = "nuttx";
pub fn configure() -> u32 {
    restore([1, 180]);
    let p = priority();
    crate::check(p == 180, "FIFO-priority");
    p
}
pub struct Threads {
    handles: Vec<std::thread::JoinHandle<()>>,
}
impl Threads {
    pub fn new() -> Self {
        Self {
            handles: Vec::new(),
        }
    }
    pub fn spawn(&mut self, entry: extern "C" fn(usize, usize), context: usize, role: usize) {
        self.handles.push(
            std::thread::Builder::new()
                .stack_size(4096)
                .spawn(move || entry(context, role))
                .unwrap(),
        );
    }
    pub fn join(self) {
        for h in self.handles {
            h.join().unwrap();
        }
    }
}

use std::{
    fs::{File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::OpenOptionsExt,
    sync::{Mutex, OnceLock},
};
static CONSOLE: OnceLock<Mutex<File>> = OnceLock::new();
unsafe extern "C" {
    fn mp_console_init() -> i32;
    fn mp_console_nonblocking() -> i32;
    fn mp_console_path() -> *const std::ffi::c_char;
}
pub fn initialize_console() {
    if unsafe { mp_console_init() } != 0 {
        fatal();
    }
    // The driver is registered before USB host configuration. Until then its
    // open operation reports ENOTCONN; acquisition must wait with a bound.
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
    let file = loop {
        match OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(unsafe { mp_console_nonblocking() })
            .open(
                unsafe { std::ffi::CStr::from_ptr(mp_console_path()) }
                    .to_str()
                    .unwrap(),
            ) {
            Ok(file) => break file,
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::NotConnected | std::io::ErrorKind::Interrupted
                ) && std::time::Instant::now() < deadline =>
            {
                std::thread::sleep(std::time::Duration::from_millis(10));
            }
            Err(_) => fatal(),
        }
    };
    if CONSOLE.set(Mutex::new(file)).is_err() {
        fatal();
    }
}
pub fn execute(words: &[&str]) {
    crate::execute_std(words);
}
pub fn read() -> Option<u8> {
    let mut b = [0];
    let result = CONSOLE.get().unwrap().lock().unwrap().read(&mut b);
    match result {
        Ok(1) => Some(b[0]),
        Ok(_) => None,
        Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => None,
        Err(_) => fatal(),
    }
}
pub fn output(mut bytes: &[u8]) {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
    while !bytes.is_empty() {
        if std::time::Instant::now() >= deadline {
            fatal();
        }
        let result = CONSOLE.get().unwrap().lock().unwrap().write(bytes);
        match result {
            Ok(n) if n > 0 => bytes = &bytes[n..],
            Err(e)
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::WouldBlock
                        | std::io::ErrorKind::NotConnected
                        | std::io::ErrorKind::BrokenPipe
                ) =>
            {
                std::thread::sleep(std::time::Duration::from_millis(1))
            }
            _ => fatal(),
        }
    }
}

pub fn main() {
    initialize_console();
    crate::mpsc_entry();
    loop {
        std::thread::sleep(std::time::Duration::from_secs(1));
    }
}
