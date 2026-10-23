//! Explicit spare-board qualification only. No device discovery or flashing.
#![deny(unsafe_op_in_unsafe_fn)]

#[cfg(target_os = "nuttx")]
mod native {
    use nxrs_csi_radio_test::echo;
    use nxrs_csi_radio_test::handoff::Handoff;
    use nxrs_csi_radio_test::lifecycle::{Lifecycle, Sdk, SdkError};
    use nxrs_csi_radio_test::traffic::Traffic;
    use nxrs_csi_radio_test::{peer, Metadata, Progress, MAX_CSI};
    use std::io::{BufWriter, Write};
    use std::net::SocketAddrV4;
    use std::sync::atomic::{AtomicBool, AtomicU32, AtomicUsize, Ordering};
    use std::sync::OnceLock;
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
    static HANDOFF: OnceLock<Handoff> = OnceLock::new();
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
        // get() never initializes or waits in the SDK callback.
        if !HANDOFF
            .get()
            .is_some_and(|h| h.push(epoch, metadata, bytes))
        {
            DROPPED.fetch_add(1, Ordering::Relaxed);
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
            HANDOFF
                .get_or_init(Handoff::new)
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
            let cleanup = self.radio.stop();
            let end = Instant::now() + Duration::from_secs(2);
            while IN_FLIGHT.load(Ordering::SeqCst) != 0 && Instant::now() < end {
                std::thread::sleep(Duration::from_millis(1));
            }
            if cleanup.is_err() || IN_FLIGHT.load(Ordering::SeqCst) != 0 {
                POISONED.store(true, Ordering::SeqCst);
                eprintln!("CSI_CLEANUP_FAILED sdk={cleanup:?}");
                return Err(Error::Io);
            }
            if HANDOFF.get().ok_or(Error::Protocol)?.retire().is_err() {
                POISONED.store(true, Ordering::SeqCst);
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

    struct CaptureOptions {
        transport_only: bool,
        tcp: bool,
    }
    fn capture(
        seconds: u64,
        bssid: [u8; 6],
        peer: SocketAddrV4,
        raw: bool,
        force: bool,
        options: CaptureOptions,
        poll: &mut dyn FnMut() -> Result<(), Error>,
    ) -> Result<(), Error> {
        let CaptureOptions {
            transport_only,
            tcp,
        } = options;
        let mut traffic = Traffic::connect(peer, tcp).map_err(|_| Error::Io)?;
        let protocol = if tcp { "tcp" } else { "udp" };
        let diagnostic = if tcp { "TCP" } else { "UDP" };
        println!("CSI_TRAFFIC protocol={protocol}");
        let mut session = if transport_only {
            None
        } else {
            Some(Session::start(bssid)?)
        };
        let start = Instant::now();
        let mut sent = 0u32;
        let mut echoed = 0u32;
        let mut factors = 0u64;
        let mut progress = Progress::default();
        let result = (|| {
            let mut fresh = start;
            let mut echoes = echo::Window::new();
            let mut next_send = start;
            let mut last_export = start;
            let mut exported = 0u64;
            let mut forced = false;
            let stdout = std::io::stdout();
            let mut output = BufWriter::new(stdout.lock());
            println!(
                "CSI_CAPTURE_BEGIN seconds={seconds} force_requested={} raw={} advisory_only=1",
                u8::from(force),
                u8::from(raw)
            );
            while start.elapsed() < Duration::from_secs(seconds) || echoes.pending() {
                poll()?;
                let now = Instant::now();
                if start.elapsed() < Duration::from_secs(seconds)
                    && now >= next_send
                    && traffic.ready()
                {
                    let data = echoes.send(start.elapsed()).map_err(|_| Error::Protocol)?;
                    let send_start = Instant::now();
                    traffic.send(data).map_err(|_| Error::Io)?;
                    if sent == 0 {
                        println!(
                            "CSI_{diagnostic}_FIRST_SEND call_us={}",
                            send_start.elapsed().as_micros()
                        );
                    }
                    sent = echoes.sent;
                    next_send = now + Duration::from_millis(100);
                }
                traffic.flush().map_err(|_| Error::Io)?;
                for _ in 0..echo::CAPACITY {
                    match traffic.receive() {
                        Ok(Some(received)) => {
                            echoes.receive(&received).map_err(|_| Error::Protocol)?;
                            echoed = echoes.echoed;
                        }
                        Ok(None) => break,
                        _ => return Err(Error::Protocol),
                    }
                }
                if let Some(sequence) = echoes.expired(start.elapsed()) {
                    eprintln!("CSI_CAPTURE_REJECT reason={protocol}_timeout sequence={sequence} frames={} sent={sent} echoes={echoed}", progress.frames);
                    return Err(Error::Timeout);
                }
                while !transport_only {
                    let frame = HANDOFF
                        .get()
                        .ok_or(Error::Protocol)?
                        .pop()
                        .map_err(|_| Error::Protocol)?;
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
                if HANDOFF.get().ok_or(Error::Protocol)?.failed()
                    || DROPPED.load(Ordering::Relaxed) != 0
                {
                    return Err(Error::Protocol);
                }
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
        let capture_end = Instant::now();
        let cleanup = session.as_mut().map_or(Ok(()), Session::stop);
        let mut handoff_result = Ok(());
        if !transport_only {
            // Snapshot outside native callbacks; no identity or raw RF in diagnostics.
            // Keep the original failure even if this read-only SDK query fails.
            let snapshot = HANDOFF.get().and_then(|h| h.snapshot().ok());
            if snapshot.is_none() {
                handoff_result = Err(Error::Protocol);
            }
            if let Some((_, overflow, malformed)) = snapshot {
                if overflow != 0 || malformed != 0 || DROPPED.load(Ordering::Relaxed) != 0 {
                    eprintln!("CSI_CAPTURE_REJECT reason=queue overflow={overflow} malformed={malformed} callback_dropped={}", DROPPED.load(Ordering::Relaxed));
                    handoff_result = Err(Error::Protocol);
                }
            }
            let mut current_peer = [0u8; 6];
            let ap_status = unsafe { nxrs_csi_test_associated_peer(current_peer.as_mut_ptr()) };
            let ap_relation = if ap_status != 0 {
                0
            } else if current_peer == bssid {
                1
            } else {
                2
            };
            eprintln!("CSI_PROGRESS elapsed_ms={} frames={} factors={factors} sent={sent} echoes={echoed} ap_status={ap_status} ap_relation={ap_relation}",capture_end.duration_since(start).as_millis(),progress.frames);
            if let Some((o, _, _)) = snapshot {
                let (age, mode, width, channel, len, matching) = o.last.map_or(
                    (u128::MAX, 255, 255, 0, 0, false),
                    |(at, mode, width, channel, len, matching)| {
                        (
                            capture_end.saturating_duration_since(at).as_millis(),
                            mode,
                            width,
                            channel,
                            len,
                            matching,
                        )
                    },
                );
                eprintln!("CSI_CALLBACKS observed={} foreign={} legacy={} ht={} other={} last_age_ms={age} last_mode={mode} last_width={width} last_channel={channel} last_len={len} last_matching={} dropped={}",o.callbacks,o.foreign,o.legacy,o.ht,o.other,u8::from(matching),DROPPED.load(Ordering::Relaxed));
            }
        }
        let socket_cleanup = (|| {
            // NuttX sends FIN asynchronously; retain the station until the peer
            // also closes. This is separate from CSI retirement and bounded.
            traffic.shutdown_write().map_err(|_| Error::Io)?;
            let deadline = Instant::now() + Duration::from_secs(2);
            loop {
                poll()?;
                if traffic.peer_closed().map_err(|_| Error::Io)? {
                    if tcp {
                        println!("CSI_TCP_CLOSED peer_eof=1");
                    }
                    return Ok(());
                }
                if Instant::now() >= deadline {
                    return Err(Error::Timeout);
                }
                std::thread::sleep(Duration::from_millis(1));
            }
        })();
        result.and(cleanup).and(handoff_result).and(socket_cleanup)
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
        if args[4..].iter().any(|a| {
            a != "--raw" && a != "--force-baseline" && a != "--transport-only" && a != "--tcp"
        }) {
            return Err(Error::InvalidConfig);
        }
        let raw = args[4..].iter().any(|a| a == "--raw");
        let force = args[4..].iter().any(|a| a == "--force-baseline");
        let transport_only = args[4..].iter().any(|a| a == "--transport-only");
        let tcp = args[4..].iter().any(|a| a == "--tcp");
        if force && args[0] == "--maintenance-camera" || transport_only && (force || raw) {
            return Err(Error::InvalidConfig);
        }
        println!(
            "CSI_TEST_OPTIONS transport_only={} raw={} force={} peer_port={} tcp={}",
            u8::from(transport_only),
            u8::from(raw),
            u8::from(force),
            host.port(),
            u8::from(tcp)
        );
        wifi_scan_owner::station::with_connection_reactor(|_, poll| {
            if transport_only {
                // No CSI configure/register/enable or numerical kernel call.
                return capture(
                    seconds,
                    [0; 6],
                    host,
                    false,
                    false,
                    CaptureOptions {
                        transport_only: true,
                        tcp,
                    },
                    poll,
                );
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
            capture(
                seconds,
                bssid,
                host,
                raw,
                force,
                CaptureOptions {
                    transport_only: false,
                    tcp,
                },
                poll,
            )
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
