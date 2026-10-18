use super::echo::{EchoSocket, Transport};
use std::net::SocketAddrV4;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};
use wifi::Error;
use wifi_scan_owner::station::Observation;

unsafe extern "C" {
    fn nxrs_radio_gatt_start() -> i32;
    fn nxrs_radio_gatt_snapshot(
        writes: *mut u32,
        reads: *mut u32,
        invalid: *mut u32,
        peer_lost: *mut u32,
    ) -> i32;
    fn nxrs_radio_gatt_stop() -> i32;
    fn nxrs_radio_gatt_wait_finish() -> i32;
    fn nxrs_radio_gatt_connections(
        connected: *mut u32,
        disconnected: *mut u32,
        native_connected: *mut u32,
        disconnect_reason: *mut i32,
    ) -> i32;
}

use super::GATT_ROUNDS;

struct TrafficRun {
    stop: Arc<AtomicBool>,
    start: Arc<AtomicBool>,
    count: Arc<AtomicU32>,
    finished: Arc<AtomicBool>,
    worker: thread::JoinHandle<Result<(), Error>>,
}

struct NotifyFinished {
    finished: Arc<AtomicBool>,
    owner: thread::Thread,
}

impl Drop for NotifyFinished {
    fn drop(&mut self) {
        // Publish before waking: JoinHandle::is_finished may still be false
        // when a higher-priority owner runs before the thread's final epilogue.
        self.finished.store(true, Ordering::Release);
        self.owner.unpark();
    }
}

impl TrafficRun {
    fn spawn(
        peer: SocketAddrV4,
        transport: Transport,
        protocol: &str,
        observation: Observation,
        counters: Option<super::stats::Counters>,
        mode: super::RunMode,
    ) -> Result<Self, Error> {
        let stop = Arc::new(AtomicBool::new(false));
        let start = Arc::new(AtomicBool::new(mode.starts_immediately()));
        let count = Arc::new(AtomicU32::new(0));
        let finished = Arc::new(AtomicBool::new(false));
        let notify = NotifyFinished {
            finished: finished.clone(),
            owner: thread::current(),
        };
        let worker_stop = Arc::clone(&stop);
        let worker_start = Arc::clone(&start);
        let worker_count = Arc::clone(&count);
        let worker = thread::Builder::new()
            .name(format!("radio-{protocol}"))
            .stack_size(8192)
            .spawn(move || {
                let _notify = notify;
                while !worker_start.load(Ordering::Acquire) {
                    if worker_stop.load(Ordering::Acquire) {
                        return Err(Error::Timeout);
                    }
                    thread::park();
                }
                // Fixed studies sample after the start gate, excluding discovery.
                // Normal qualification retains its pre-advertising baseline.
                let counters = counters.unwrap_or_else(|| {
                    super::stats::Counters::start(
                        peer,
                        matches!(transport, Transport::Udp | Transport::NativeUdp),
                    )
                });
                traffic(
                    peer,
                    transport,
                    &worker_stop,
                    &worker_count,
                    observation,
                    counters,
                    mode.packet_limit(),
                )
            })
            .map_err(|_| Error::Unavailable)?;
        Ok(Self {
            stop,
            start,
            count,
            finished,
            worker,
        })
    }

    fn begin(&self) {
        self.start.store(true, Ordering::Release);
        self.worker.thread().unpark();
    }

    fn finish(self) -> (Result<(), Error>, u32) {
        self.stop.store(true, Ordering::Release);
        self.worker.thread().unpark();
        let result = self
            .worker
            .join()
            .map_err(|_| Error::Io)
            .and_then(|value| value);
        (result, self.count.load(Ordering::Acquire))
    }
}

pub(super) fn run() -> Result<(), Error> {
    let mut args = std::env::args().skip(1);
    let peer = args.next().ok_or(Error::InvalidConfig)?;
    if peer == "ble-client" {
        let mode = ble_client_fixture::Mode::from_args(args).ok_or(Error::InvalidConfig)?;
        // Reuse the standalone oracle without acquiring Wi-Fi or a traffic worker.
        return if ble_client_fixture::run(mode, |_| Ok(())) {
            Ok(())
        } else {
            Err(Error::Io)
        };
    }
    if peer == "ble-only" {
        if args.next().is_some() {
            return Err(Error::InvalidConfig);
        }
        return ble_only();
    }
    if matches!(peer.as_str(), "ble-values" | "ble-cancel") {
        if args.next().is_some() {
            return Err(Error::InvalidConfig);
        }
        return if peer == "ble-values" {
            ble_values()
        } else {
            ble_cancel()
        };
    }
    let peer: SocketAddrV4 = peer.parse().map_err(|_| Error::InvalidConfig)?;
    if peer.ip().is_unspecified() || peer.ip().is_multicast() || peer.port() == 0 {
        return Err(Error::InvalidConfig);
    }
    let protocol = args.next().unwrap_or_else(|| "udp".into());
    let transport = Transport::parse(&protocol).ok_or(Error::InvalidConfig)?;
    let mode = args.next();
    let mode = super::RunMode::parse(mode.as_deref()).ok_or(Error::InvalidConfig)?;
    if args.next().is_some() {
        return Err(Error::InvalidConfig);
    }
    if mode == super::RunMode::PortableClient {
        wifi_scan_owner::station::with_connection_reactor(|observation, poll| {
            portable_client(peer, transport, &protocol, observation, poll)
        })
    } else {
        wifi_scan_owner::station::with_connection_observed(|observation| {
            if mode == super::RunMode::WifiOnly {
                wifi_only(peer, transport, &protocol, observation)
            } else {
                coexist(peer, transport, &protocol, observation, mode)
            }
        })
    }?;
    println!("NXRS_RADIO_PASS wifi=disconnected worker=joined");
    Ok(())
}

fn ble_only() -> Result<(), Error> {
    // Same GATT fixture and deadline, but no Wi-Fi lease or traffic worker.
    start_gatt(None)?;
    finish_ble_only()
}

fn ble_values() -> Result<(), Error> {
    start_gatt(None)?;
    finish_ble_only()
}

fn finish_ble_only() -> Result<(), Error> {
    println!("NXRS_RADIO_GATT_READY rounds={GATT_ROUNDS}");
    let result = super::finish_gatt(
        observe(None, super::RunMode::Qualification),
        wait_gatt_finish,
    );
    if result.is_err() {
        report_gatt_connections("failure");
    }
    let stopped = stop_gatt(None);
    result?;
    stopped?;
    println!("NXRS_RADIO_PASS wifi=not-started worker=not-started");
    Ok(())
}

fn ble_cancel() -> Result<(), Error> {
    start_gatt(None)?;
    println!("NXRS_RADIO_GATT_READY mode=cancel");
    let result = observe_cancellation();
    if result.is_err() {
        report_gatt_connections("failure");
    }
    // Native callback removal is required even after an observation failure.
    let stopped = stop_gatt(None);
    result?;
    stopped?;
    println!("NXRS_RADIO_CANCEL_PASS connected=1 disconnected=1 writes=0 reads=0 invalid=0 cleanup=confirmed");
    Ok(())
}

fn observe_cancellation() -> Result<(), Error> {
    let deadline = Instant::now() + Duration::from_secs(45);
    loop {
        let (mut writes, mut reads, mut invalid, mut lost) = (0, 0, 0, 0);
        let (mut connected, mut disconnected, mut native, mut reason) = (0, 0, 0, -1);
        // SAFETY: the helpers write only these fixed-width, call-local scalars.
        let snapshot =
            unsafe { nxrs_radio_gatt_snapshot(&mut writes, &mut reads, &mut invalid, &mut lost) };
        let link = unsafe {
            nxrs_radio_gatt_connections(&mut connected, &mut disconnected, &mut native, &mut reason)
        };
        if snapshot != 0 || link != 0 {
            return Err(Error::Io);
        }
        if super::cancellation_complete(writes, reads, invalid, connected, disconnected, native)? {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err(Error::Timeout);
        }
        thread::sleep(Duration::from_millis(10));
    }
}

fn wifi_only(
    peer: SocketAddrV4,
    transport: Transport,
    protocol: &str,
    observation: Observation,
) -> Result<(), Error> {
    let traffic = TrafficRun::spawn(
        peer,
        transport,
        protocol,
        observation,
        None,
        super::RunMode::WifiOnly,
    )?;
    println!("NXRS_RADIO_CONTROL_READY");
    let deadline = Instant::now() + Duration::from_secs(60);
    while !traffic.worker.is_finished() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    let expired = !traffic.worker.is_finished();
    let (result, count) = traffic.finish();
    result?;
    if expired {
        return Err(Error::Timeout);
    }
    if count != super::CONTROL_PACKETS {
        return Err(Error::Protocol);
    }
    println!("NXRS_RADIO_CONTROL_TRAFFIC_DONE");
    println!("NXRS_RADIO_TRAFFIC_PASS gatt=0 {protocol}={count}");
    Ok(())
}

fn portable_client(
    peer: SocketAddrV4,
    transport: Transport,
    protocol: &str,
    observation: Observation,
    poll_wifi: &mut dyn FnMut() -> Result<(), Error>,
) -> Result<(), Error> {
    let traffic = TrafficRun::spawn(
        peer,
        transport,
        protocol,
        observation,
        None,
        super::RunMode::PortableClient,
    )?;
    let mut overlap = super::client::Overlap::default();
    let mut background_error = None;
    let passed = ble_client_fixture::run(ble_client_fixture::Mode::Full, |progress| {
        poll_wifi().map_err(|error| {
            background_error = Some(error);
            "wifi_reactor"
        })?;
        if traffic.finished.load(Ordering::Acquire) {
            return Err("traffic_stopped");
        }
        if overlap
            .record(progress, traffic.count.load(Ordering::Acquire))
            .map_err(|_| "traffic_overlap")?
        {
            traffic.begin();
        }
        Ok(())
    });
    // This also releases the start gate after scan/connect failure. Always join
    // before propagating the original scenario error; no detached traffic.
    let (traffic_result, count) = traffic.finish();
    if let Some(error) = background_error {
        return Err(error);
    }
    if !passed {
        return Err(Error::Protocol);
    }
    traffic_result?;
    let span = overlap.finish()?;
    println!(
        "NXRS_RADIO_CLIENT_OVERLAP first_ms={} last_ms={} first_echoes={} last_echoes={}",
        span.first_ms, span.last_ms, span.first_echoes, span.last_echoes
    );
    println!("NXRS_RADIO_TRAFFIC_PASS gatt={GATT_ROUNDS} {protocol}={count}");
    Ok(())
}

fn coexist(
    peer: SocketAddrV4,
    transport: Transport,
    protocol: &str,
    observation: Observation,
    mode: super::RunMode,
) -> Result<(), Error> {
    // Take firmware counters before advertising/traffic, never from a driver
    // callback. Queries may use the native CDC timeout if unsupported.
    let counters = (mode == super::RunMode::Qualification).then(|| {
        super::stats::Counters::start(
            peer,
            matches!(transport, Transport::Udp | Transport::NativeUdp),
        )
    });
    start_gatt(Some(&observation))?;
    let worker = TrafficRun::spawn(
        peer,
        transport,
        protocol,
        observation.clone(),
        counters,
        mode,
    );
    let (result, count) = match worker {
        Ok(worker) => {
            println!("NXRS_RADIO_GATT_READY rounds={GATT_ROUNDS}");
            let result = super::finish_gatt(observe(Some(&worker), mode), wait_gatt_finish);
            let (traffic, count) = worker.finish();
            (result.and(traffic), count)
        }
        Err(error) => (Err(error), 0),
    };
    // Stop advertising and reject new fixture traffic even after an error.
    if result.is_err() {
        report_gatt_connections("failure");
    }
    let stopped = stop_gatt(Some(&observation));
    result?;
    stopped?;
    if mode
        .packet_limit()
        .is_some_and(|expected| count != expected)
    {
        return Err(Error::Protocol);
    }
    println!(
        "NXRS_RADIO_TRAFFIC_PASS gatt={GATT_ROUNDS} {protocol}={}",
        count
    );
    Ok(())
}

fn start_gatt(observation: Option<&Observation>) -> Result<(), Error> {
    // SAFETY: no retained Rust pointers. The C fixture owns static callbacks.
    let started = unsafe { nxrs_radio_gatt_start() };
    if started != 0 {
        eprintln!("NXRS_RADIO_NATIVE_FAIL stage=gattstart code={started}");
        if let Some(observation) = observation {
            observation.report("gattstart");
        }
        // Static callbacks remain valid even after partial start failure.
        let _ = stop_gatt(observation);
        return Err(Error::Io);
    }
    Ok(())
}

fn wait_gatt_finish() -> Result<(), Error> {
    println!("NXRS_RADIO_GATT_DONE");
    // The host confirms its disconnect before native shutdown.
    let finished = unsafe { nxrs_radio_gatt_wait_finish() };
    if finished != 0 {
        eprintln!("NXRS_RADIO_NATIVE_FAIL stage=gattfinish code={finished}");
        return Err(Error::Timeout);
    }
    Ok(())
}

fn stop_gatt(observation: Option<&Observation>) -> Result<(), Error> {
    // Report failed cleanup even when an earlier operation error wins.
    let stopped = unsafe { nxrs_radio_gatt_stop() };
    if stopped != 0 {
        eprintln!("NXRS_RADIO_NATIVE_FAIL stage=gattstop code={stopped}");
        if let Some(observation) = observation {
            observation.report("gattstop");
        }
        return Err(Error::Io);
    }
    Ok(())
}

fn report_gatt_connections(stage: &str) {
    let (mut connected, mut disconnected, mut native_connected) = (0, 0, 0);
    let mut disconnect_reason = -1;
    // SAFETY: fixed-width output scalars; the native fixture retains no pointers.
    let result = unsafe {
        nxrs_radio_gatt_connections(
            &mut connected,
            &mut disconnected,
            &mut native_connected,
            &mut disconnect_reason,
        )
    };
    if result != 0 {
        println!("NXRS_RADIO_GATT_LINK stage={stage} available=0 code={result}");
        return;
    }
    println!(
        "NXRS_RADIO_GATT_LINK stage={stage} available=1 connected_events={connected} \
         disconnected_events={disconnected} native_connected={native_connected} code=0 \
         last_disconnect_reason={disconnect_reason}"
    );
}

fn observe(traffic: Option<&TrafficRun>, mode: super::RunMode) -> Result<bool, Error> {
    let deadline = Instant::now() + Duration::from_secs(60);
    let mut next_link_report = Instant::now() + Duration::from_secs(5);
    let mut first = None;
    loop {
        let (mut writes, mut reads, mut invalid, mut peer_lost) = (0, 0, 0, 0);
        // SAFETY: fixed-width writable scalars, used only for this call.
        if unsafe {
            nxrs_radio_gatt_snapshot(&mut writes, &mut reads, &mut invalid, &mut peer_lost)
        } != 0
        {
            return Err(Error::Io);
        }
        let complete = super::gatt_complete(
            writes,
            reads,
            invalid,
            peer_lost != 0,
            mode == super::RunMode::Qualification
                || traffic.is_none_or(|traffic| traffic.worker.is_finished()),
        );
        if peer_lost != 0 && complete == Err(Error::Io) {
            println!("NXRS_RADIO_GATT_PEER_LOST writes={writes} reads={reads}");
        }
        let complete = complete?;
        let current = traffic.map_or(0, |traffic| traffic.count.load(Ordering::Acquire));
        if writes == 0 && Instant::now() >= next_link_report {
            report_gatt_connections("waiting");
            next_link_report = Instant::now() + Duration::from_secs(5);
        }
        if writes > 0 && first.is_none() {
            report_gatt_connections("first-write");
            first = Some(current);
            if let Some(traffic) = traffic {
                traffic.begin();
            }
        }
        if complete {
            // Network completions must advance while GATT is active. Fixed
            // studies keep reading the final value until traffic finishes.
            // Completion and overlap are separate: even a failed overlap check
            // must let the host disconnect after receiving its final readback.
            if mode == super::RunMode::CoexistControl {
                // Completion barrier, not success: join still preserves errors.
                println!("NXRS_RADIO_CONTROL_TRAFFIC_DONE");
            }
            return Ok(traffic.is_none() || first.is_some_and(|first| current > first + 5));
        }
        if Instant::now() >= deadline {
            return Err(Error::Timeout);
        }
        thread::sleep(Duration::from_millis(10));
    }
}

fn traffic(
    peer: SocketAddrV4,
    transport: Transport,
    stop: &AtomicBool,
    count: &AtomicU32,
    observation: Observation,
    mut counters: super::stats::Counters,
    limit: Option<u32>,
) -> Result<(), Error> {
    let mut socket = EchoSocket::open(peer, transport).map_err(|error| {
        eprintln!(
            "NXRS_RADIO_TRAFFIC_FAIL phase=connect sequence=0 kind={:?}",
            error.kind()
        );
        Error::Io
    })?;
    let (mut sent, mut received) = ([0; 512], [0; 513]);
    let mut sequence = 0u32;
    while !stop.load(Ordering::Acquire) && limit.is_none_or(|limit| sequence < limit) {
        let started = Instant::now();
        let size = [16, 64, 256, 512][sequence as usize % 4];
        sent[..4].copy_from_slice(&sequence.to_be_bytes());
        for (offset, byte) in sent[4..size].iter_mut().enumerate() {
            *byte = (offset as u8).wrapping_add(sequence as u8);
        }
        if socket.send(&mut sent[..size]).map_err(|error| {
            println!(
                "NXRS_RADIO_TRAFFIC_TIMING sequence={sequence} elapsed_ms={}",
                started.elapsed().as_millis().min(u32::MAX as u128) as u32
            );
            socket.report_tcp_state(sequence, "send-failure");
            counters.report_interface("send-failure");
            counters.report_trace(sequence, "send-failure");
            eprintln!(
                "NXRS_RADIO_TRAFFIC_FAIL phase=send sequence={sequence} kind={:?}",
                error.kind()
            );
            Error::Io
        })? != size
        {
            return Err(Error::Protocol);
        }
        let length = match socket.receive(&mut received, size) {
            Ok(length) => length,
            Err(error) => {
                let failed_at = Instant::now();
                socket.report_tcp_state(sequence, "immediate");
                // Compare the whole expected frame, not a stale prefix from
                // the previous receive. No buffer contents are printed.
                let payload_match = received[..size] == sent[..size];
                // Sample this socket before close, not the global protocol totals.
                socket.report_receive_queue(sequence, "immediate", 0);
                counters.report_interface("immediate");
                counters.report_trace(sequence, "immediate");
                if sequence > 0 {
                    counters.report_trace(sequence - 1, "previous");
                }
                observation.report("immediate");
                println!(
                    "NXRS_RADIO_TRAFFIC_TIMING sequence={sequence} elapsed_ms={}",
                    started.elapsed().as_millis().min(u32::MAX as u128) as u32
                );
                println!(
                    "NXRS_RADIO_RX_BUFFER sequence={sequence} payload_match={}",
                    u32::from(payload_match)
                );
                eprintln!(
                    "NXRS_RADIO_TRAFFIC_FAIL phase=receive sequence={sequence} kind={:?}",
                    error.kind()
                );
                // The operation has already failed. Observe a late arrival without
                // consuming it, retrying the echo or changing its two-second budget.
                thread::sleep(Duration::from_millis(250));
                socket.report_tcp_state(sequence, "late");
                socket.report_receive_queue(
                    sequence,
                    "late",
                    failed_at.elapsed().as_millis().min(u32::MAX as u128) as u32,
                );
                counters.report_interface("late");
                counters.report_trace(sequence, "late");
                observation.report("late");
                // Do blocking controller probes only after the immediate/late
                // socket samples, so their timing remains independent of CDC.
                counters.report_controller("failure");
                // A controller query wakes the bus daemon. Record whether it
                // exposed a late pending frame; the receive stays failed.
                counters.report_trace(sequence, "postquery");
                return Err(Error::Timeout);
            }
        };
        if received[..length] != sent[..size] {
            return Err(Error::Protocol);
        }
        let elapsed = started.elapsed();
        if elapsed >= Duration::from_secs(1) {
            println!(
                "NXRS_RADIO_TRAFFIC_DELAY sequence={sequence} elapsed_ms={}",
                elapsed.as_millis().min(u32::MAX as u128) as u32
            );
            counters.report("delay");
        }
        sequence = sequence.checked_add(1).ok_or(Error::Protocol)?;
        count.store(sequence, Ordering::Release);
        thread::sleep(Duration::from_millis(10));
    }
    socket.finish().map_err(|error| {
        eprintln!(
            "NXRS_RADIO_TRAFFIC_FAIL phase=shutdown sequence={sequence} kind={:?}",
            error.kind()
        );
        Error::Io
    })
}
