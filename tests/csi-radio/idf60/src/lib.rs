//! Isolated SDK reference. Rust owns capture, station lifetime and TCP stimulus.
#[path = "../../src/lib.rs"]
#[allow(dead_code)] // Reuse the exact qualified capture core, including unused fixture cases.
mod capture;
use capture::{echo, handoff::Handoff, traffic::Traffic, Progress, MAX_CSI};
pub use capture::{Frame, Metadata, Observation, Queue, QUEUE_SIZE};
use std::ffi::c_void;
use std::io::{BufWriter, Write};
use std::net::SocketAddrV4;
use std::sync::{
    atomic::{AtomicBool, AtomicU32, Ordering},
    OnceLock,
};
use std::time::{Duration, Instant};

type Result<T> = std::result::Result<T, &'static str>;
type Event = unsafe extern "C" fn(*mut c_void, *const i8, i32, *mut c_void);
type Callback = unsafe extern "C" fn(*mut c_void, *mut c_void);
#[cfg(target_os = "espidf")]
mod abi {
    #[no_mangle]
    pub static reference_rust_abi: [u32; 51] = [
        std::mem::size_of::<libc::time_t>() as u32,
        std::mem::size_of::<libc::timespec>() as u32,
        std::mem::size_of::<libc::timeval>() as u32,
        std::mem::size_of::<libc::sockaddr_in>() as u32,
        std::mem::offset_of!(libc::sockaddr_in, sin_family) as u32,
        std::mem::offset_of!(libc::sockaddr_in, sin_port) as u32,
        std::mem::size_of::<libc::pollfd>() as u32,
        std::mem::size_of::<libc::pthread_mutex_t>() as u32,
        std::mem::size_of::<libc::pthread_cond_t>() as u32,
        std::mem::size_of::<libc::socklen_t>() as u32,
        libc::AF_INET as u32,
        libc::SOCK_STREAM as u32,
        libc::SOL_SOCKET as u32,
        libc::SO_RCVTIMEO as u32,
        libc::SO_SNDTIMEO as u32,
        libc::IPPROTO_TCP as u32,
        libc::TCP_NODELAY as u32,
        libc::O_NONBLOCK as u32,
        libc::FIONBIO as u32,
        libc::CLOCK_MONOTONIC as u32,
        std::mem::size_of::<libc::pthread_attr_t>() as u32,
        std::mem::size_of::<libc::sockaddr>() as u32,
        std::mem::align_of::<libc::pthread_mutex_t>() as u32,
        std::mem::align_of::<libc::pthread_cond_t>() as u32,
        std::mem::size_of::<libc::pthread_rwlock_t>() as u32,
        std::mem::align_of::<libc::pthread_rwlock_t>() as u32,
        std::mem::size_of::<libc::pthread_mutexattr_t>() as u32,
        std::mem::align_of::<libc::pthread_mutexattr_t>() as u32,
        std::mem::size_of::<libc::pthread_condattr_t>() as u32,
        std::mem::align_of::<libc::pthread_condattr_t>() as u32,
        std::mem::align_of::<libc::pthread_attr_t>() as u32,
        std::mem::size_of::<libc::pthread_t>() as u32,
        libc::POLLIN as u32,
        libc::POLLOUT as u32,
        libc::POLLERR as u32,
        libc::POLLHUP as u32,
        libc::POLLNVAL as u32,
        libc::EAGAIN as u32,
        libc::EWOULDBLOCK as u32,
        libc::EINTR as u32,
        libc::ETIMEDOUT as u32,
        libc::EINVAL as u32,
        libc::ECANCELED as u32,
        libc::PTHREAD_MUTEX_NORMAL as u32,
        libc::PTHREAD_MUTEX_ERRORCHECK as u32,
        unsafe {
            std::mem::transmute::<libc::pthread_mutex_t, u32>(libc::PTHREAD_MUTEX_INITIALIZER)
        },
        unsafe { std::mem::transmute::<libc::pthread_cond_t, u32>(libc::PTHREAD_COND_INITIALIZER) },
        std::mem::size_of::<libc::nfds_t>() as u32,
        std::mem::offset_of!(libc::pollfd, events) as u32,
        std::mem::offset_of!(libc::pollfd, revents) as u32,
        std::mem::align_of::<libc::time_t>() as u32,
    ];
}
extern "C" {
    fn reference_quiet();
    fn reference_usb_install() -> i32;
    fn reference_usb_stdio();
    fn reference_usb_read(data: *mut u8, length: usize) -> i32;
    fn reference_netif_init() -> i32;
    fn reference_loop_create() -> i32;
    fn reference_loop_delete() -> i32;
    fn reference_netif_create() -> *mut c_void;
    fn reference_netif_destroy(netif: *mut c_void);
    fn reference_register_wifi(callback: Event, handle: *mut *mut c_void) -> i32;
    fn reference_register_ip(callback: Event, handle: *mut *mut c_void) -> i32;
    fn reference_unregister_wifi(handle: *mut c_void) -> i32;
    fn reference_unregister_ip(handle: *mut c_void) -> i32;
    fn reference_event_kind(base: *const i8, id: i32) -> i32;
    fn reference_wifi_init() -> i32;
    fn reference_ram_storage() -> i32;
    fn reference_sta_mode() -> i32;
    fn reference_wifi_config(ssid: *const u8, slen: usize, password: *const u8, plen: usize)
        -> i32;
    fn reference_no_power_save() -> i32;
    fn reference_ap(peer: *mut u8, channel: *mut u8) -> i32;
    fn reference_csi_config() -> i32;
    fn reference_translate(info: *mut c_void, metadata: *mut Metadata) -> *const i8;
    fn esp_wifi_start() -> i32;
    fn esp_wifi_connect() -> i32;
    fn esp_wifi_disconnect() -> i32;
    fn esp_wifi_stop() -> i32;
    fn esp_wifi_deinit() -> i32;
    fn esp_wifi_set_csi(enabled: bool) -> i32;
    fn esp_wifi_set_csi_rx_cb(callback: Option<Callback>, context: *mut c_void) -> i32;
    fn esp_csi_gain_ctrl_reset_rx_gain_baseline();
    fn esp_csi_gain_ctrl_record_rx_gain(agc: u8, fft: i8) -> i32;
    fn esp_csi_gain_ctrl_get_gain_compensation(factor: *mut f32, agc: u8, fft: i8) -> i32;
}
static HANDOFF: OnceLock<Handoff> = OnceLock::new();
static ACTIVE: AtomicU32 = AtomicU32::new(0);
static IN_FLIGHT: AtomicU32 = AtomicU32::new(0);
static DROPPED: AtomicU32 = AtomicU32::new(0);
static ADDRESSED: AtomicBool = AtomicBool::new(false);
static DISCONNECTED: AtomicBool = AtomicBool::new(false);
static POISONED: AtomicBool = AtomicBool::new(false);

pub(crate) fn checked(status: i32) -> Result<()> {
    if status == 0 {
        Ok(())
    } else {
        eprintln!("IDF60_SDK_ERROR status={status}");
        Err("sdk")
    }
}
unsafe extern "C" fn event(_: *mut c_void, base: *const i8, id: i32, _: *mut c_void) {
    match reference_event_kind(base, id) {
        1 => ADDRESSED.store(true, Ordering::Release),
        2 => {
            DISCONNECTED.store(true, Ordering::Release);
            ADDRESSED.store(false, Ordering::Release);
        }
        _ => (),
    }
}
unsafe extern "C" fn callback(context: *mut c_void, info: *mut c_void) {
    IN_FLIGHT.fetch_add(1, Ordering::SeqCst);
    let epoch = context as usize as u32;
    if epoch != 0 && ACTIVE.load(Ordering::SeqCst) == epoch {
        let mut metadata: Metadata = std::mem::zeroed();
        let bytes = reference_translate(info, &mut metadata);
        if bytes.is_null() || metadata.len == 0 || usize::from(metadata.len) > MAX_CSI {
            DROPPED.fetch_add(1, Ordering::Relaxed);
        } else if !HANDOFF.get().unwrap().push(
            epoch,
            metadata,
            std::slice::from_raw_parts(bytes, usize::from(metadata.len)),
        ) {
            DROPPED.fetch_add(1, Ordering::Relaxed);
        }
    }
    IN_FLIGHT.fetch_sub(1, Ordering::SeqCst);
}

pub(crate) struct Station {
    event_loop: bool,
    netif: *mut c_void,
    wifi_event: *mut c_void,
    ip_event: *mut c_void,
    initialized: bool,
    started: bool,
    csi_registered: bool,
}
impl Station {
    pub(crate) fn connect(ssid: &[u8], password: &[u8]) -> Result<Self> {
        let mut s = Self {
            event_loop: false,
            netif: std::ptr::null_mut(),
            wifi_event: std::ptr::null_mut(),
            ip_event: std::ptr::null_mut(),
            initialized: false,
            started: false,
            csi_registered: false,
        };
        unsafe {
            checked(reference_loop_create())?;
            s.event_loop = true;
            s.netif = reference_netif_create();
            if s.netif.is_null() {
                return Err("netif");
            }
            checked(reference_register_wifi(event, &mut s.wifi_event))?;
            checked(reference_register_ip(event, &mut s.ip_event))?;
            checked(reference_wifi_init())?;
            s.initialized = true;
            checked(reference_ram_storage())?;
            checked(reference_sta_mode())?;
            checked(reference_wifi_config(
                ssid.as_ptr(),
                ssid.len(),
                password.as_ptr(),
                password.len(),
            ))?;
            ADDRESSED.store(false, Ordering::Release);
            DISCONNECTED.store(false, Ordering::Release);
            checked(esp_wifi_start())?;
            s.started = true;
            checked(reference_no_power_save())?;
            checked(esp_wifi_connect())?;
        }
        let at = Instant::now();
        while !ADDRESSED.load(Ordering::Acquire) {
            if DISCONNECTED.load(Ordering::Acquire) || at.elapsed() > Duration::from_secs(25) {
                return Err("association");
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        Ok(s)
    }
    fn capture(&mut self, epoch: u32, peer: [u8; 6]) -> Result<()> {
        HANDOFF.get().unwrap().begin(epoch, peer)?;
        DROPPED.store(0, Ordering::Relaxed);
        unsafe {
            esp_csi_gain_ctrl_reset_rx_gain_baseline();
            checked(reference_csi_config())?;
            checked(esp_wifi_set_csi_rx_cb(
                Some(callback),
                epoch as usize as *mut c_void,
            ))?;
            self.csi_registered = true;
            ACTIVE.store(epoch, Ordering::SeqCst);
            checked(esp_wifi_set_csi(true))?;
        }
        Ok(())
    }
    fn retire(&mut self) -> Result<()> {
        if !self.csi_registered {
            return Ok(());
        }
        ACTIVE.store(0, Ordering::SeqCst);
        let disabled = unsafe { esp_wifi_set_csi(false) };
        let unregistered = unsafe { esp_wifi_set_csi_rx_cb(None, std::ptr::null_mut()) };
        let at = Instant::now();
        while IN_FLIGHT.load(Ordering::SeqCst) != 0 && at.elapsed() < Duration::from_secs(2) {
            std::thread::sleep(Duration::from_millis(1));
        }
        if disabled != 0 || unregistered != 0 || IN_FLIGHT.load(Ordering::SeqCst) != 0 {
            POISONED.store(true, Ordering::Release);
            return Err("callback_retirement");
        }
        HANDOFF.get().unwrap().retire()?;
        self.csi_registered = false;
        Ok(())
    }
    fn close(&mut self) -> Result<()> {
        self.retire()?; // Retain SDK resources if callback quiescence is uncertain.
        let mut ok = true;
        unsafe {
            if self.started {
                let status = esp_wifi_disconnect();
                ok &= status == 0;
                let at = Instant::now();
                while !DISCONNECTED.load(Ordering::Acquire) && at.elapsed() < Duration::from_secs(2)
                {
                    std::thread::sleep(Duration::from_millis(5));
                }
                ok &= DISCONNECTED.load(Ordering::Acquire);
                ok &= esp_wifi_stop() == 0;
                self.started = false;
            }
            if self.initialized {
                ok &= esp_wifi_deinit() == 0;
                self.initialized = false;
            }
            if !self.ip_event.is_null() {
                ok &= reference_unregister_ip(self.ip_event) == 0;
                self.ip_event = std::ptr::null_mut();
            }
            if !self.wifi_event.is_null() {
                ok &= reference_unregister_wifi(self.wifi_event) == 0;
                self.wifi_event = std::ptr::null_mut();
            }
            if !self.netif.is_null() {
                reference_netif_destroy(self.netif);
                self.netif = std::ptr::null_mut();
            }
            if self.event_loop {
                ok &= reference_loop_delete() == 0;
                self.event_loop = false;
            }
        }
        if ok {
            Ok(())
        } else {
            POISONED.store(true, Ordering::Release);
            Err("station_cleanup")
        }
    }
}
impl Drop for Station {
    fn drop(&mut self) {
        let _ = self.close();
    }
}

pub(crate) fn read_exact(bytes: &mut [u8], deadline: Instant) -> Result<()> {
    let mut read = 0;
    while read < bytes.len() {
        if Instant::now() >= deadline {
            return Err("input_timeout");
        }
        let n = unsafe { reference_usb_read(bytes[read..].as_mut_ptr(), bytes.len() - read) };
        if n < 0 {
            return Err("input");
        }
        read += n as usize;
    }
    Ok(())
}
fn command() -> Result<(u64, SocketAddrV4, bool)> {
    println!("IDF60_COMMAND_READY force=0");
    std::io::stdout().flush().map_err(|_| "output")?;
    let mut line = [0; 128];
    let deadline = Instant::now() + Duration::from_secs(90);
    for i in 0..line.len() {
        read_exact(&mut line[i..i + 1], deadline)?;
        if line[i] == b'\n' {
            let text = std::str::from_utf8(&line[..i]).map_err(|_| "command")?;
            let args: Vec<_> = text.split_whitespace().collect();
            if args.len() != 4 || args[0] != "IDF60" {
                return Err("command");
            }
            let seconds = args[1].parse::<u64>().map_err(|_| "command")?;
            let peer = args[2].parse::<SocketAddrV4>().map_err(|_| "command")?;
            if !(30..=120).contains(&seconds)
                || peer.port() == 0
                || peer.ip().is_unspecified()
                || peer.ip().is_multicast()
            {
                return Err("command");
            }
            let transport = match args[3] {
                "capture" => false,
                "transport" => true,
                _ => return Err("command"),
            };
            return Ok((seconds, peer, transport));
        }
    }
    Err("command_length")
}
fn trial(epoch: u32) -> Result<()> {
    let (seconds, peer, transport) = command()?;
    println!(
        "IDF60_OPTIONS seconds={seconds} transport_only={} tcp=1 raw={} force=0 peer_port={}",
        u8::from(transport),
        u8::from(!transport),
        peer.port()
    );
    println!("IDF60_CREDENTIALS_READY");
    std::io::stdout().flush().map_err(|_| "output")?;
    let deadline = Instant::now() + Duration::from_secs(60);
    let mut lengths = [0; 2];
    read_exact(&mut lengths, deadline)?;
    let (slen, plen) = (lengths[0] as usize, lengths[1] as usize);
    if slen == 0 || slen > 32 || plen > 63 || (plen != 0 && plen < 8) {
        return Err("credentials_length");
    }
    let mut credentials = [0; 95];
    read_exact(&mut credentials[..slen + plen], deadline)?;
    let station = Station::connect(&credentials[..slen], &credentials[slen..slen + plen]);
    for byte in &mut credentials {
        unsafe {
            std::ptr::write_volatile(byte, 0);
        }
    }
    let mut station = station?;
    let mut bssid = [0; 6];
    let mut channel = 0;
    unsafe {
        checked(reference_ap(bssid.as_mut_ptr(), &mut channel))?;
    }
    // Private host evidence only; never include the AP identity in public receipts.
    print!("IDF60_PEER bssid=");
    for byte in bssid {
        print!("{byte:02x}");
    }
    println!(" channel={channel}");
    std::io::stdout().flush().map_err(|_| "output")?;
    let mut traffic = Traffic::connect(peer, true).map_err(|_| "tcp_connect")?;
    if !transport {
        station.capture(epoch, bssid)?;
    }
    let start = Instant::now();
    let mut fresh = start;
    let mut next_send = start;
    let mut export = start;
    let mut echoes = echo::Window::new();
    let mut progress = Progress::default();
    let mut factors = 0u64;
    let mut exported = 0u64;
    let stdout = std::io::stdout();
    let mut output = BufWriter::new(stdout.lock());
    writeln!(
        output,
        "IDF60_BEGIN seconds={seconds} channel={channel} transport_only={}",
        u8::from(transport)
    )
    .map_err(|_| "output")?;
    output.flush().map_err(|_| "output")?;
    while start.elapsed() < Duration::from_secs(seconds) || echoes.pending() {
        if !ADDRESSED.load(Ordering::Acquire) || DISCONNECTED.load(Ordering::Acquire) {
            return Err("link_lost");
        }
        let now = Instant::now();
        if start.elapsed() < Duration::from_secs(seconds) && now >= next_send && traffic.ready() {
            traffic
                .send(echoes.send(start.elapsed()).map_err(|_| "echo_window")?)
                .map_err(|_| "tcp_send")?;
            next_send = now + Duration::from_millis(100);
        }
        traffic.flush().map_err(|_| "tcp_flush")?;
        for _ in 0..echo::CAPACITY {
            match traffic.receive() {
                Ok(Some(packet)) => echoes.receive(&packet).map_err(|_| "echo_mismatch")?,
                Ok(None) => break,
                Err(_) => return Err("tcp_receive"),
            }
        }
        if echoes.expired(start.elapsed()).is_some() {
            return Err("echo_timeout");
        }
        if !transport {
            for _ in 0..QUEUE_SIZE {
                let Some(frame) = HANDOFF.get().unwrap().pop()? else {
                    break;
                };
                let m = frame.metadata;
                if !progress.observe(&m) || m.channel != channel {
                    return Err("rf_progress");
                }
                fresh = frame.received_at;
                if fresh.elapsed() >= Duration::from_secs(2) {
                    return Err("queued_stale");
                }
                unsafe {
                    checked(esp_csi_gain_ctrl_record_rx_gain(m.agc_gain, m.fft_gain))?;
                    if progress.frames >= 50 {
                        let mut factor = 0.0;
                        checked(esp_csi_gain_ctrl_get_gain_compensation(
                            &mut factor,
                            m.agc_gain,
                            m.fft_gain,
                        ))?;
                        if !factor.is_finite() || factor <= 0.0 {
                            return Err("gain_factor");
                        }
                        factors += 1;
                    }
                }
                if export.elapsed() >= Duration::from_millis(100) {
                    write!(output,"CSI_RAW ts_us={} channel={} secondary={} len={} invalid_first={} rssi={} agc={} fft={} rx=",m.timestamp_us,m.channel,m.secondary_channel,m.len,m.first_word_invalid,m.rssi,m.agc_gain,m.fft_gain).map_err(|_|"output")?;
                    for b in &m.rx_ctrl[..usize::from(m.rx_ctrl_len)] {
                        write!(output, "{b:02x}").map_err(|_| "output")?;
                    }
                    write!(output, " csi=").map_err(|_| "output")?;
                    for b in &frame.data[..usize::from(m.len)] {
                        write!(output, "{:02x}", *b as u8).map_err(|_| "output")?;
                    }
                    writeln!(output).map_err(|_| "output")?;
                    output.flush().map_err(|_| "output")?;
                    exported += 1;
                    export = Instant::now();
                }
            }
            if fresh.elapsed() >= Duration::from_secs(2) {
                return Err("rf_timeout");
            }
            if HANDOFF.get().unwrap().failed() || DROPPED.load(Ordering::Relaxed) != 0 {
                return Err("callback_drop");
            }
        }
        std::thread::sleep(Duration::from_millis(1));
    }
    let elapsed = start.elapsed().as_millis();
    station.retire()?;
    if !transport {
        let (obs, overflow, malformed) = HANDOFF.get().unwrap().snapshot()?;
        if overflow != 0
            || malformed != 0
            || DROPPED.load(Ordering::Relaxed) != 0
            || progress.frames < 50
            || factors == 0
        {
            return Err("capture_acceptance");
        }
        writeln!(
            output,
            "IDF60_CALLBACKS observed={} foreign={} legacy={} ht={} other={} dropped=0",
            obs.callbacks, obs.foreign, obs.legacy, obs.ht, obs.other
        )
        .map_err(|_| "output")?;
    }
    traffic.shutdown_write().map_err(|_| "tcp_shutdown")?;
    let at = Instant::now();
    while !traffic.peer_closed().map_err(|_| "tcp_eof")? {
        if at.elapsed() >= Duration::from_secs(2) {
            return Err("tcp_eof_timeout");
        }
        std::thread::sleep(Duration::from_millis(1));
    }
    drop(traffic);
    station.close()?;
    writeln!(output,"IDF60_RESULT elapsed_ms={elapsed} frames={} factors={factors} sent={} echoes={} raw_exported={exported} force=0 transport_only={}",progress.frames,echoes.sent,echoes.echoed,u8::from(transport)).map_err(|_|"output")?;
    writeln!(
        output,
        "IDF60_PASS capture_cleanup=confirmed tcp_eof=1 station_disconnected=1 sdk_deinitialized=1"
    )
    .map_err(|_| "output")?;
    output.flush().map_err(|_| "output")
}

#[no_mangle]
pub extern "C" fn reference_main() {
    std::panic::set_hook(Box::new(|_| eprintln!("IDF60_FAIL reason=panic")));
    let initialization = unsafe {
        checked(reference_usb_install()).and_then(|_| {
            reference_usb_stdio();
            reference_quiet();
            checked(reference_netif_init())
        })
    };
    HANDOFF.get_or_init(Handoff::new);
    if initialization.is_ok() {
        for epoch in 1..=3 {
            if POISONED.load(Ordering::Acquire) {
                break;
            }
            if let Err(reason) = trial(epoch) {
                eprintln!("IDF60_FAIL reason={reason}");
                break;
            }
        }
    } else {
        eprintln!("IDF60_FAIL reason=initialization");
    }
    loop {
        std::thread::sleep(Duration::from_secs(1));
    }
}
