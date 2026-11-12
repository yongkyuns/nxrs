//! Test-local NuttX witnesses. No production task launcher or radio policy.
use std::ffi::{c_char, c_void, CStr, CString};
use std::io::{self, Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::PathBuf;
use std::time::{Duration, Instant};

#[path = "console.rs"]
mod local;
use local::Console;

// A failed maintenance write cannot justify releasing a live native child.
// Keep ownership and local input available; missing host markers fail the run.
macro_rules! local_out {
    ($console:expr, $($argument:tt)*) => {{
        let _ = writeln!($console.output, $($argument)*);
    }};
}
macro_rules! local_error {
    ($console:expr, $($argument:tt)*) => {{
        let _ = writeln!($console.error, $($argument)*);
    }};
}

unsafe extern "C" {
    fn nxrs_ssh_test_spawn(action: *const c_char) -> i32;
    fn nxrs_ssh_test_wait(pid: i32, result: *mut i32) -> i32;
    fn nxrs_ssh_test_readable(timeout_ms: i32) -> i32;
    fn nxrs_ssh_test_winsize(rows: *mut u16, columns: *mut u16) -> i32;
    fn nxrs_ssh_test_raw_begin() -> *mut c_void;
    fn nxrs_ssh_test_raw_end(state: *mut c_void, discard: i32) -> i32;
    fn nxrs_ssh_test_key_path() -> *const c_char;
    fn nxrs_ssh_test_passwd_path() -> *const c_char;
    fn nxrs_ssh_test_adduser(user: *const c_char, password: *const c_char) -> i32;
    fn nxrs_ssh_test_interrupt_begin() -> *mut c_void;
    fn nxrs_ssh_test_interrupt_seen() -> i32;
    fn nxrs_ssh_test_interrupt_end(state: *mut c_void) -> i32;
}

fn native_error(value: i32) -> io::Error {
    io::Error::from_raw_os_error(value.abs())
}

pub fn finish_local(result: io::Result<()>) {
    if let Err(error) = result {
        if let Ok(mut console) = Console::stdio() {
            let _ = writeln!(console.error, "NXRS_SSH_PROBE_FAIL error={error}");
        }
        std::process::exit(1);
    }
}

fn spawn(action: &str) -> io::Result<i32> {
    let action = CString::new(action).unwrap();
    // SAFETY: exec_builtin copies the argv strings into the child task.
    let result = unsafe { nxrs_ssh_test_spawn(action.as_ptr()) };
    if result < 0 {
        Err(native_error(result))
    } else {
        Ok(result)
    }
}

fn reaped(pid: i32) -> io::Result<Option<i32>> {
    let mut result = 0;
    // SAFETY: one owning lab task calls wait for this child; output is writable.
    match unsafe { nxrs_ssh_test_wait(pid, &mut result) } {
        0 => Ok(None),
        1 => Ok(Some(result)),
        error => Err(native_error(error)),
    }
}

fn readable(timeout: i32) -> io::Result<bool> {
    // SAFETY: thin poll translation touches only the calling task's stdin.
    match unsafe { nxrs_ssh_test_readable(timeout) } {
        0 => Ok(false),
        1 => Ok(true),
        error => Err(native_error(error)),
    }
}

pub fn terminal_size() -> io::Result<(u16, u16)> {
    let (mut rows, mut columns) = (0, 0);
    // SAFETY: writable fixed-width outputs; ioctl layout remains in C.
    let result = unsafe { nxrs_ssh_test_winsize(&mut rows, &mut columns) };
    if result == 0 {
        Ok((rows, columns))
    } else {
        Err(native_error(result))
    }
}

pub fn wait_for_interrupt(token: &str) -> io::Result<()> {
    // SAFETY: this test owns the SIGINT disposition until the matching restore.
    let state = unsafe { nxrs_ssh_test_interrupt_begin() };
    if state.is_null() {
        return Err(io::Error::last_os_error());
    }
    let result = (|| {
        let mut output = io::stdout();
        writeln!(output, "NXRS_SSH_WAIT token={token}")?;
        output.flush()?;
        let deadline = Instant::now() + Duration::from_secs(60);
        // SIGINT only records a flag. All I/O and exit run on this Rust task.
        while unsafe { nxrs_ssh_test_interrupt_seen() } == 0 {
            if Instant::now() >= deadline {
                writeln!(output, "NXRS_SSH_WAIT_EXPIRED token={token}")?;
                return Err(io::ErrorKind::TimedOut.into());
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        // Stop can close the PTY while the interrupted child is returning.
        // Keep the write fallible so a hangup still reaches signal cleanup.
        writeln!(output, "NXRS_SSH_INTERRUPTED token={token}")?;
        output.flush()
    })();
    // SAFETY: restore and free the one saved disposition exactly once.
    let restore = unsafe { nxrs_ssh_test_interrupt_end(state) };
    if restore != 0 {
        return Err(native_error(restore));
    }
    result
}

fn proc_text(path: &str) -> io::Result<String> {
    let mut info = String::new();
    std::fs::File::open(path)?
        .take(8193)
        .read_to_string(&mut info)?;
    if info.len() > 8192 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "proc observation exceeded bound",
        ));
    }
    Ok(info)
}

fn snapshot(console: &mut Console, ticks: u64) -> io::Result<()> {
    writeln!(console.output, "NXRS_SSH_SNAPSHOT_BEGIN ticks={ticks}")?;
    let info = proc_text("/proc/meminfo")?;
    writeln!(console.output, "{info}")?;
    let mut tasks: Vec<u32> = std::fs::read_dir("/proc")?
        .filter_map(|entry| entry.ok()?.file_name().to_str()?.parse().ok())
        .collect();
    tasks.sort_unstable();
    writeln!(
        console.output,
        "NXRS_SSH_TASKS count={} ids={tasks:?}",
        tasks.len()
    )?;
    // Do not read cmdline/environ/password paths into evidence.
    for pid in tasks {
        for name in ["stack", "group/fd"] {
            let path = format!("/proc/{pid}/{name}");
            match proc_text(&path) {
                Ok(text) => {
                    writeln!(console.output, "NXRS_SSH_PROC path={path}\n{text}")?;
                }
                Err(error) => writeln!(
                    console.output,
                    "NXRS_SSH_PROC_UNAVAILABLE pid={pid} item={name} kind={:?}",
                    error.kind()
                )?,
            }
        }
    }
    writeln!(console.output, "NXRS_SSH_SNAPSHOT_END ticks={ticks}")?;
    console.output.flush()
}

pub fn lab() -> io::Result<()> {
    let mut console = Console::stdio()?;
    wifi_scan_owner::station::with_connection_reactor(|_, poll_wifi| {
        let mut server = None;
        let mut helper: Option<(i32, String)> = None;
        let mut quit = false;
        let mut fault = None;
        let mut ticks = 0u64;
        local_out!(
            console,
            "NXRS_SSH_LAB_READY commands=start,status,stop,snapshot,progress,provision,quit"
        );
        loop {
            ticks = ticks.saturating_add(1);
            if let Err(error) = poll_wifi() {
                if fault.is_none() {
                    local_error!(console, "NXRS_SSH_LAB_WIFI_FAIL error={error:?}");
                    fault = Some(error);
                    quit = true;
                }
            }
            if let Some(pid) = server {
                match reaped(pid) {
                    Ok(Some(result)) => {
                        server = None;
                        local_out!(console, "NXRS_SSH_LAB_SERVER_EXIT result={result}");
                    }
                    Ok(None) => {}
                    Err(error) => {
                        // Do not release the network or fabricate a joined task.
                        local_error!(console, "NXRS_SSH_LAB_WAIT_FAIL role=server error={error}");
                        std::thread::sleep(Duration::from_millis(100));
                    }
                }
            }
            if let Some((pid, action)) = &helper {
                match reaped(*pid) {
                    Ok(Some(result)) => {
                        local_out!(
                            console,
                            "NXRS_SSH_LAB_CONTROL action={action} result={result}"
                        );
                        helper = None;
                        if result != 0 {
                            quit = false;
                        }
                    }
                    Ok(None) => {}
                    Err(error) => {
                        local_error!(console, "NXRS_SSH_LAB_WAIT_FAIL role=control error={error}");
                    }
                }
            }
            if quit && helper.is_none() {
                if server.is_none() {
                    return fault.map_or(Ok(()), Err);
                }
                match spawn("stop") {
                    Ok(pid) => helper = Some((pid, "stop".into())),
                    Err(error) => {
                        local_error!(console, "NXRS_SSH_LAB_SPAWN_FAIL action=stop error={error}");
                        quit = false; // Keep local recovery control and network alive.
                    }
                }
            }
            match readable(100) {
                Ok(false) => continue,
                Err(error) => {
                    local_error!(console, "NXRS_SSH_LAB_CONSOLE_FAIL error={error}");
                    quit = true;
                    std::thread::sleep(Duration::from_millis(100));
                    continue;
                }
                Ok(true) => {}
            }
            let line = match console.line() {
                Ok(line) => line,
                Err(error) => {
                    local_error!(console, "NXRS_SSH_LAB_CONSOLE_FAIL error={error}");
                    quit = true;
                    continue;
                }
            };
            match line.trim() {
                "start" if server.is_none() && helper.is_none() => match spawn("start") {
                    Ok(pid) => {
                        server = Some(pid);
                        local_out!(console, "NXRS_SSH_LAB_STARTED pid={pid}");
                    }
                    Err(error) => local_error!(
                        console,
                        "NXRS_SSH_LAB_SPAWN_FAIL action=start error={error}"
                    ),
                },
                action @ ("status" | "stop") if helper.is_none() => match spawn(action) {
                    Ok(pid) => helper = Some((pid, action.into())),
                    Err(error) => {
                        local_error!(
                            console,
                            "NXRS_SSH_LAB_SPAWN_FAIL action={action} error={error}"
                        )
                    }
                },
                "snapshot" => {
                    if let Err(error) = snapshot(&mut console, ticks) {
                        local_error!(console, "NXRS_SSH_SNAPSHOT_FAIL error={error}");
                    }
                }
                "progress" => local_out!(console, "NXRS_SSH_LAB_PROGRESS ticks={ticks}"),
                "provision" if server.is_none() && helper.is_none() => {
                    if let Err(error) = provision_on(&mut console) {
                        local_error!(console, "NXRS_SSH_PROVISION_FAIL error={error}");
                    }
                }
                "quit" => quit = true,
                "" => {}
                _ => local_out!(console, "NXRS_SSH_LAB_REJECTED reason=invalid_or_busy"),
            }
            let _ = console.output.flush();
        }
    })
    .map_err(|error| io::Error::other(format!("Wi-Fi owner: {error:?}")))?;
    writeln!(
        console.output,
        "NXRS_SSH_LAB_DONE native=joined wifi=disconnected_and_joined"
    )?;
    Ok(())
}

struct RawInput {
    state: *mut c_void,
    error: std::fs::File,
}

impl RawInput {
    fn begin(error: std::fs::File) -> io::Result<Self> {
        // SAFETY: the returned termios allocation belongs to this guard.
        let value = unsafe { nxrs_ssh_test_raw_begin() };
        if value.is_null() {
            Err(io::Error::last_os_error())
        } else {
            Ok(Self {
                state: value,
                error,
            })
        }
    }

    fn finish(mut self) -> io::Result<()> {
        let state = std::mem::replace(&mut self.state, std::ptr::null_mut());
        // SAFETY: consume the unique saved termios allocation once.
        let result = unsafe { nxrs_ssh_test_raw_end(state, 1) };
        if result == 0 {
            Ok(())
        } else {
            Err(native_error(result))
        }
    }
}

impl Drop for RawInput {
    fn drop(&mut self) {
        if self.state.is_null() {
            return;
        }
        // Always discard unread bytes before restoring console echo.
        // SAFETY: this consumes the one saved termios allocation exactly once.
        let result = unsafe { nxrs_ssh_test_raw_end(self.state, 1) };
        if result != 0 {
            let _ = writeln!(
                self.error,
                "NXRS_SSH_PROVISION_CONSOLE_RESTORE_FAIL errno={result}"
            );
        }
    }
}

fn receive(input: &mut std::fs::File, buffer: &mut [u8], deadline: Instant) -> io::Result<()> {
    let mut received = 0;
    while received < buffer.len() {
        if Instant::now() >= deadline {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "secret input deadline",
            ));
        }
        if readable(100)? {
            let count = input.read(&mut buffer[received..])?;
            if count == 0 {
                return Err(io::ErrorKind::UnexpectedEof.into());
            }
            received += count;
        }
    }
    Ok(())
}

pub fn provision() -> io::Result<()> {
    provision_on(&mut Console::stdio()?)
}

fn provision_on(console: &mut Console) -> io::Result<()> {
    // Provision once from the trusted physical console with the daemon stopped.
    // The filesystem/mount is selected by the operator; no claim of persistence
    // follows merely from creating a path or returning from sync_all().
    let (key_path, passwd_path) = unsafe {
        // SAFETY: these C functions return static configured strings or NULL.
        let key = nxrs_ssh_test_key_path();
        let passwd = nxrs_ssh_test_passwd_path();
        if key.is_null() || passwd.is_null() {
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "SSH is disabled",
            ));
        }
        (
            PathBuf::from(CStr::from_ptr(key).to_str().map_err(io::Error::other)?),
            PathBuf::from(CStr::from_ptr(passwd).to_str().map_err(io::Error::other)?),
        )
    };
    if key_path.try_exists()? || passwd_path.try_exists()? {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "identity or account database already exists",
        ));
    }
    let raw = RawInput::begin(console.error.try_clone()?)?;
    writeln!(console.output, "NXRS_SSH_PROVISION_READY")?;
    console.output.flush()?;
    let deadline = Instant::now() + Duration::from_secs(60);
    let mut header = [0u8; 7];
    receive(&mut console.input, &mut header, deadline)?;
    let key_length = u32::from_be_bytes(header[..4].try_into().unwrap()) as usize;
    let user_length = header[4] as usize;
    let password_length = u16::from_be_bytes(header[5..7].try_into().unwrap()) as usize;
    if !(32..=4096).contains(&key_length)
        || !(1..=32).contains(&user_length)
        || !(8..=256).contains(&password_length)
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "invalid provision frame",
        ));
    }
    let mut frame = vec![0; key_length + user_length + password_length];
    let result = (|| {
        receive(&mut console.input, &mut frame, deadline)?;
        let user_bytes = &frame[key_length..key_length + user_length];
        if !user_bytes
            .iter()
            .all(|value| value.is_ascii_alphanumeric() || b"_-".contains(value))
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid account name",
            ));
        }
        let user = CString::new(user_bytes).map_err(io::Error::other)?;
        let mut password = frame[key_length + user_length..].to_vec();
        if !password.iter().all(|value| (0x20..=0x7e).contains(value)) {
            password.fill(0);
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid password encoding",
            ));
        }
        password.push(0);
        for path in [&key_path, &passwd_path] {
            std::fs::create_dir_all(
                path.parent()
                    .ok_or_else(|| io::Error::other("missing parent"))?,
            )?;
        }
        let mut key = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&key_path)?;
        let provisioned = (|| {
            key.write_all(&frame[..key_length])?;
            key.sync_all()?;
            // SAFETY: NUL-terminated strings stay alive for this native call.
            let result = unsafe { nxrs_ssh_test_adduser(user.as_ptr(), password.as_ptr().cast()) };
            if result != 0 {
                return Err(native_error(result));
            }
            std::fs::File::open(&passwd_path)?.sync_all()?;
            Ok(())
        })();
        password.fill(0);
        drop(key);
        if provisioned.is_err() {
            // Both paths were absent on entry. Do not silently retain a partial
            // identity after a provisioning failure; cleanup errors stay visible.
            for path in [&key_path, &passwd_path] {
                if path.try_exists()? {
                    std::fs::remove_file(path)?;
                }
            }
        }
        provisioned
    })();
    frame.fill(0); // Best effort; not a guarantee about every stack/native copy.
    let restored = raw.finish();
    result?;
    restored?;
    writeln!(
        console.output,
        "NXRS_SSH_PROVISION_DONE identity=new password_db=native"
    )?;
    Ok(())
}
