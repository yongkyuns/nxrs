//! Explicit spare-board qualification only. No device discovery or flashing.
#![deny(unsafe_op_in_unsafe_fn)]

#[cfg(target_os = "nuttx")]
mod native {
    use nxrs_csi_radio_test::lifecycle::{Lifecycle, Sdk, SdkError};
    use nxrs_csi_radio_test::{peer, Metadata, Progress, Queue, MAX_CSI};
    use std::io::{BufWriter, Write};
    use std::net::{SocketAddrV4, UdpSocket};
    use std::sync::atomic::{AtomicBool, AtomicU32, AtomicUsize, Ordering};
    use std::sync::Mutex;
    use std::time::{Duration, Instant};
    use wifi::Error;

    #[repr(C)]
    struct NativeCsiInfo {
        _private: [u8; 0],
    }

    unsafe extern "C" {
        fn nxrs_csi_test_configure() -> i32;
        fn nxrs_csi_test_associated_peer(peer: *mut u8) -> i32;
        fn nxrs_csi_test_register(
            epoch: u32,
            callback: unsafe extern "C" fn(*mut std::ffi::c_void, *mut NativeCsiInfo),
        ) -> i32;
        fn nxrs_csi_test_translate(
            info: *const NativeCsiInfo,
            metadata: *mut Metadata,
        ) -> *const i8;
        fn nxrs_csi_test_enable(enabled: bool) -> i32;
        fn nxrs_csi_test_force(agc: u8, fft: i8) -> i32;
        fn nxrs_csi_test_record(agc: u8, fft: i8) -> i32;
        fn nxrs_csi_test_baseline(agc: *mut u8, fft: *mut i8) -> i32;
        fn nxrs_csi_test_compensation(factor: *mut f32, agc: u8, fft: i8) -> i32;
        fn nxrs_csi_test_reset();
    }
    // SDK callback storage has process lifetime, including after failed stop.
    // Late callbacks carry an epoch and cannot enter a later capture session.
    static QUEUE: Mutex<Queue> = Mutex::new(Queue::new());
    static ACTIVE: AtomicU32 = AtomicU32::new(0);
    static NEXT: AtomicU32 = AtomicU32::new(1);
    static IN_FLIGHT: AtomicUsize = AtomicUsize::new(0);
    static DROPPED: AtomicUsize = AtomicUsize::new(0);
    static POISONED: AtomicBool = AtomicBool::new(false);
    struct Callback;
    impl Drop for Callback {
        fn drop(&mut self) {
            IN_FLIGHT.fetch_sub(1, Ordering::SeqCst);
        }
    }

    unsafe extern "C" fn received(context: *mut std::ffi::c_void, info: *mut NativeCsiInfo) {
        IN_FLIGHT.fetch_add(1, Ordering::SeqCst);
        let _callback = Callback;
        let epoch = context as usize as u32;
        if epoch == 0 || ACTIVE.load(Ordering::SeqCst) != epoch {
            return;
        }
        let mut metadata = std::mem::MaybeUninit::<Metadata>::uninit();
        // SAFETY: the SDK owns info until this native callback returns. The
        // C translator uses the pinned headers, initializes all metadata on a
        // non-null result, and returns the SDK's borrowed payload. No pointer
        // is retained; queue admission copies bounded bytes before returning.
        let bytes = unsafe { nxrs_csi_test_translate(info, metadata.as_mut_ptr()) };
        if bytes.is_null() {
            DROPPED.fetch_add(1, Ordering::Relaxed);
            return;
        }
        let metadata = unsafe { metadata.assume_init() };
        if usize::from(metadata.len) > MAX_CSI {
            DROPPED.fetch_add(1, Ordering::Relaxed);
            return;
        }
        let bytes = unsafe { std::slice::from_raw_parts(bytes, usize::from(metadata.len)) };
        match QUEUE.try_lock() {
            Ok(mut queue) => queue.push(epoch, metadata, bytes),
            Err(_) => {
                DROPPED.fetch_add(1, Ordering::Relaxed);
            }
        }
    }
    fn checked(status: i32) -> Result<(), Error> {
        if status == 0 {
            Ok(())
        } else {
            eprintln!("CSI_SDK_ERROR status={status}");
            Err(Error::Io)
        }
    }
    struct NativeSdk;
    impl Sdk for NativeSdk {
        fn configure(&mut self) -> Result<(), SdkError> {
            sdk_result(unsafe { nxrs_csi_test_configure() })
        }
        fn register(&mut self, epoch: u32) -> Result<(), SdkError> {
            sdk_result(unsafe { nxrs_csi_test_register(epoch, received) })
        }
        fn enable(&mut self, enabled: bool) -> Result<(), SdkError> {
            sdk_result(unsafe { nxrs_csi_test_enable(enabled) })
        }
        fn force(&mut self, agc: u8, fft: i8) -> Result<(), SdkError> {
            sdk_result(unsafe { nxrs_csi_test_force(agc, fft) })
        }
    }
    fn sdk_result(status: i32) -> Result<(), SdkError> {
        if status == 0 {
            Ok(())
        } else {
            Err(SdkError(status))
        }
    }
    struct Session {
        radio: Lifecycle<NativeSdk>,
    }
    impl Session {
        fn start(bssid: [u8; 6]) -> Result<Self, Error> {
            if POISONED.load(Ordering::SeqCst) {
                return Err(Error::Unavailable);
            }
            let epoch = NEXT
                .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_add(1))
                .map_err(|_| Error::Unavailable)?;
            QUEUE
                .lock()
                .map_err(|_| Error::Protocol)?
                .begin(epoch, bssid)
                .map_err(|_| Error::Busy)?;
            DROPPED.store(0, Ordering::SeqCst);
            ACTIVE.store(epoch, Ordering::SeqCst);
            unsafe {
                nxrs_csi_test_reset();
            }
            let mut session = Self {
                radio: Lifecycle::new(NativeSdk),
            };
            if let Err(error) = session.radio.start(epoch) {
                eprintln!("CSI_START_FAILED error={error:?}");
                let _ = session.stop();
                return Err(Error::Io);
            }
            Ok(session)
        }
        fn force(&mut self, agc: u8, fft: i8) -> Result<(), Error> {
            self.radio.force(agc, fft).map_err(|error| {
                eprintln!("CSI_FORCE_FAILED error={error:?}");
                Error::Io
            })
        }
        fn stop(&mut self) -> Result<(), Error> {
            if ACTIVE.swap(0, Ordering::SeqCst) == 0 {
                return if POISONED.load(Ordering::SeqCst) {
                    Err(Error::Io)
                } else {
                    Ok(())
                };
            }
            let queue_ok = match QUEUE.lock() {
                Ok(mut queue) => {
                    queue.end();
                    true
                }
                Err(_) => false,
            };
            let cleanup = self.radio.stop();
            let end = Instant::now() + Duration::from_secs(2);
            while IN_FLIGHT.load(Ordering::SeqCst) != 0 && Instant::now() < end {
                std::thread::sleep(Duration::from_millis(1));
            }
            if !queue_ok || cleanup.is_err() || IN_FLIGHT.load(Ordering::SeqCst) != 0 {
                POISONED.store(true, Ordering::SeqCst);
                eprintln!("CSI_CLEANUP_FAILED sdk={cleanup:?}");
                return Err(Error::Io);
            }
            Ok(())
        }
    }
    impl Drop for Session {
        fn drop(&mut self) {
            let _ = self.stop();
        }
    }

    fn capture(
        seconds: u64,
        bssid: [u8; 6],
        peer: SocketAddrV4,
        raw: bool,
        force: bool,
        transport_only: bool,
        poll: &mut dyn FnMut() -> Result<(), Error>,
    ) -> Result<(), Error> {
        let socket =
            UdpSocket::bind((std::net::Ipv4Addr::UNSPECIFIED, 0)).map_err(|_| Error::Io)?;
        socket.connect(peer).map_err(|_| Error::Io)?;
        socket.set_nonblocking(true).map_err(|_| Error::Io)?;
        let mut session = if transport_only {
            None
        } else {
            Some(Session::start(bssid)?)
        };
        let result = (|| {
            let start = Instant::now();
            let mut fresh = start;
            let mut progress = Progress::default();
            let mut sent = 0u32;
            let mut echoed = 0u32;
            let mut pending = None;
            let mut next_send = start;
            let mut last_export = start;
            let mut exported = 0u64;
            let mut factors = 0u64;
            let mut forced = false;
            let stdout = std::io::stdout();
            let mut output = BufWriter::new(stdout.lock());
            println!(
                "CSI_CAPTURE_BEGIN seconds={seconds} force_requested={} raw={} advisory_only=1",
                u8::from(force),
                u8::from(raw)
            );
            while start.elapsed() < Duration::from_secs(seconds) || pending.is_some() {
                poll()?;
                let now = Instant::now();
                if start.elapsed() < Duration::from_secs(seconds)
                    && pending.is_none()
                    && now >= next_send
                {
                    let mut data = [0u8; 16];
                    data[..4].copy_from_slice(&sent.to_be_bytes());
                    data[4..].copy_from_slice(b"NXRS-CSIRAD!");
                    let send_start = Instant::now();
                    if socket.send(&data).map_err(|_| Error::Io)? != data.len() {
                        return Err(Error::Io);
                    }
                    if sent == 0 {
                        println!(
                            "CSI_UDP_FIRST_SEND call_us={}",
                            send_start.elapsed().as_micros()
                        );
                    }
                    pending = Some((data, now));
                    sent += 1;
                    next_send = now + Duration::from_millis(100);
                }
                if let Some((expected, when)) = pending {
                    let mut received = [0u8; 17];
                    match socket.recv(&mut received) {
                        Ok(16) if received[..16] == expected => {
                            echoed += 1;
                            pending = None;
                        }
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {}
                        _ => return Err(Error::Protocol),
                    }
                    if pending.is_some() && when.elapsed() >= Duration::from_secs(2) {
                        eprintln!("CSI_CAPTURE_REJECT reason=udp_timeout sequence={} frames={} sent={sent} echoes={echoed}", sent - 1, progress.frames);
                        return Err(Error::Timeout);
                    }
                }
                while !transport_only {
                    let frame = QUEUE.lock().map_err(|_| Error::Protocol)?.pop();
                    let Some(frame) = frame else { break };
                    let m = frame.metadata;
                    if !progress.observe(&m) {
                        eprintln!("CSI_CAPTURE_REJECT reason=rf_progress");
                        return Err(Error::Protocol);
                    }
                    fresh = frame.received_at;
                    if fresh.elapsed() >= Duration::from_secs(2) {
                        eprintln!("CSI_CAPTURE_REJECT reason=queued_stale");
                        return Err(Error::Timeout);
                    }
                    unsafe {
                        checked(nxrs_csi_test_record(m.agc_gain, m.fft_gain))?;
                        if progress.frames >= 50 {
                            let mut factor = 0f32;
                            checked(nxrs_csi_test_compensation(
                                &mut factor,
                                m.agc_gain,
                                m.fft_gain,
                            ))?;
                            if !factor.is_finite() || factor <= 0.0 {
                                return Err(Error::Protocol);
                            }
                            factors += 1;
                            if force && !forced {
                                let (mut agc, mut fft) = (0, 0);
                                checked(nxrs_csi_test_baseline(&mut agc, &mut fft))?;
                                if agc == 0 && fft == 0 {
                                    return Err(Error::Protocol);
                                }
                                session.as_mut().ok_or(Error::Protocol)?.force(agc, fft)?;
                                forced = true;
                                writeln!(output, "CSI_FORCE_BASELINE agc={agc} fft={fft}")
                                    .map_err(|_| Error::Io)?;
                            }
                        }
                    }
                    // Explicit export sampling, distinct from queue loss. No I/O in callback.
                    if raw && last_export.elapsed() >= Duration::from_millis(100) {
                        write!(output,"CSI_RAW ts_us={} channel={} secondary={} len={} invalid_first={} rssi={} agc={} fft={} rx=",m.timestamp_us,m.channel,m.secondary_channel,m.len,m.first_word_invalid,m.rssi,m.agc_gain,m.fft_gain).map_err(|_| Error::Io)?;
                        for b in &m.rx_ctrl[..usize::from(m.rx_ctrl_len)] {
                            write!(output, "{b:02x}").map_err(|_| Error::Io)?;
                        }
                        write!(output, " csi=").map_err(|_| Error::Io)?;
                        for b in &frame.data[..usize::from(m.len)] {
                            write!(output, "{:02x}", *b as u8).map_err(|_| Error::Io)?;
                        }
                        writeln!(output).map_err(|_| Error::Io)?;
                        output.flush().map_err(|_| Error::Io)?;
                        last_export = Instant::now();
                        exported += 1;
                    }
                }
                if transport_only {
                    std::thread::sleep(Duration::from_millis(1));
                    continue;
                }
                let queue = QUEUE.lock().map_err(|_| Error::Protocol)?;
                if queue.overflow != 0
                    || queue.malformed != 0
                    || DROPPED.load(Ordering::Relaxed) != 0
                {
                    eprintln!("CSI_CAPTURE_REJECT reason=queue overflow={} malformed={} callback_dropped={}", queue.overflow, queue.malformed, DROPPED.load(Ordering::Relaxed));
                    return Err(Error::Protocol);
                }
                drop(queue);
                if fresh.elapsed() >= Duration::from_secs(2) {
                    eprintln!(
                        "CSI_CAPTURE_REJECT reason=rf_timeout frames={}",
                        progress.frames
                    );
                    return Err(Error::Timeout);
                }
                std::thread::sleep(Duration::from_millis(1));
            }
            if transport_only {
                if echoed < 10 || sent != echoed {
                    return Err(Error::Unavailable);
                }
                println!("CSI_TRANSPORT_RESULT echoes={echoed} csi_configured=0");
                return Ok(());
            }
            if progress.frames < 50
                || factors == 0
                || echoed < 10
                || sent != echoed
                || force && !forced
            {
                return Err(Error::Unavailable);
            }
            writeln!(output,"CSI_CAPTURE_RESULT frames={} factors={factors} echoes={echoed} raw_exported={exported} force_exercised={} loss=0 advisory_only=1",progress.frames,u8::from(forced)).map_err(|_| Error::Io)?;
            output.flush().map_err(|_| Error::Io)
        })();
        let cleanup = session.as_mut().map_or(Ok(()), Session::stop);
        result.and(cleanup)
    }
    pub fn run() -> Result<(), Error> {
        let args: Vec<_> = std::env::args().skip(1).collect();
        if args.len() < 4 || (args[0] != "--spare-board" && args[0] != "--maintenance-camera") {
            return Err(Error::InvalidConfig);
        }
        let seconds: u64 = args[1].parse().map_err(|_| Error::InvalidConfig)?;
        if !(10..=300).contains(&seconds) {
            return Err(Error::InvalidConfig);
        }
        let bssid = if args[2] == "associated" {
            None
        } else {
            Some(peer(&args[2]).map_err(|_| Error::InvalidConfig)?)
        };
        let host: SocketAddrV4 = args[3].parse().map_err(|_| Error::InvalidConfig)?;
        if host.ip().is_unspecified()
            || host.ip().is_multicast()
            || host.ip().is_loopback()
            || host.port() == 0
        {
            return Err(Error::InvalidConfig);
        }
        if args[4..]
            .iter()
            .any(|a| a != "--raw" && a != "--force-baseline" && a != "--transport-only")
        {
            return Err(Error::InvalidConfig);
        }
        let raw = args[4..].iter().any(|a| a == "--raw");
        let force = args[4..].iter().any(|a| a == "--force-baseline");
        let transport_only = args[4..].iter().any(|a| a == "--transport-only");
        if force && args[0] == "--maintenance-camera" || transport_only && (force || raw) {
            return Err(Error::InvalidConfig);
        }
        println!(
            "CSI_TEST_OPTIONS transport_only={} raw={} force={} peer_port={}",
            u8::from(transport_only),
            u8::from(raw),
            u8::from(force),
            host.port()
        );
        wifi_scan_owner::station::with_connection_reactor(|_, poll| {
            if transport_only {
                // No CSI configure/register/enable or numerical kernel call.
                return capture(seconds, [0; 6], host, false, false, true, poll);
            }
            let bssid = match bssid {
                Some(peer) => peer,
                None => {
                    let mut peer = [0u8; 6];
                    checked(unsafe { nxrs_csi_test_associated_peer(peer.as_mut_ptr()) })?;
                    if peer == [0; 6] || peer[0] & 1 != 0 {
                        return Err(Error::Protocol);
                    }
                    peer
                }
            };
            capture(seconds, bssid, host, raw, force, false, poll)
        })?;
        if transport_only {
            println!("CSI_TRANSPORT_PASS cleanup=confirmed station=disconnected worker=joined advisory_only=1");
        } else {
            println!("CSI_RADIO_PASS cleanup=confirmed station=disconnected worker=joined advisory_only=1");
        }
        Ok(())
    }
}
fn main() {
    #[cfg(target_os = "nuttx")]
    if let Err(error) = native::run() {
        eprintln!("CSI_RADIO_FAIL error={error:?}");
        std::process::exit(1);
    }
    #[cfg(not(target_os = "nuttx"))]
    {
        eprintln!("This binary requires the explicit spare-board NuttX test image. Host: cargo test --lib.");
        std::process::exit(2);
    }
}
