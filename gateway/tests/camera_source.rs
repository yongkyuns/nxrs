//! Native test owner for the ACTUAL service/stream implementation.
//! Only image generation/control is synthetic; no HTTP or multipart writer lives here.
#![forbid(unsafe_code)]
use camera::{Capture, Format, Frame, PixelFormat};
use std::{env, fs, io::{self, BufRead, Write}, net::TcpListener, sync::mpsc,
    thread, time::{Duration, Instant}};
use stream_service::{Options, Server};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let paths: Vec<_> = env::args_os().skip(1).collect();
    if paths.len() != 2 { return Err("two synthetic JPEG paths required".into()); }
    let images: Vec<_> = paths.iter().map(fs::read).collect::<io::Result<_>>()?;
    if images.iter().any(|b: &Vec<u8>| b.len() > 153_600 || b.len() < 4) {
        return Err("invalid synthetic image size".into());
    }
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let mut server = Server::new(listener, vec![0u8; 153_600], Options::default())?;
    let (send, recv) = mpsc::sync_channel(1);
    let input = thread::spawn(move || {
        for line in io::stdin().lock().lines() {
            let Ok(line) = line else { break };
            let quit = line == "quit";
            if line.len() > 16 || send.send(line).is_err() || quit { break; }
        }
    });
    let mut out = io::stdout().lock();
    writeln!(out, "{{\"port\":{},\"implementation\":\"rust-service-stream\"}}", server.local_addr()?.port())?;
    out.flush()?;
    let started = Instant::now();
    let mut sequence = 0u64;
    let mut capture_ms = 0;
    let mut next_frame = 0;
    let mut paused = false;
    let mut active = false;
    let mut peak = 0u64;
    loop {
        let now = u64::try_from(started.elapsed().as_millis())?;
        match recv.try_recv() {
            Ok(command) => {
                let quit = command == "quit";
                match command.as_str() {
                    "pause" => paused = true,
                    "resume" => { paused = false; sequence = 0; next_frame = now; }
                    "status" => {}
                    "quit" => { server.disconnect(); active = false; }
                    _ => return Err("invalid fixture control command".into()),
                }
                let stats = server.stats();
                writeln!(out, "{{\"active\":{},\"peak\":{},\"connections\":{},\"disconnected\":{},\"frames\":{},\"busy\":{}}}",
                    usize::from(active), peak, stats.accepted, stats.disconnected,
                    stats.frames_completed, stats.rejected_busy)?;
                out.flush()?;
                if quit { break; }
            }
            Err(mpsc::TryRecvError::Disconnected) => { server.disconnect(); break; }
            Err(mpsc::TryRecvError::Empty) => {}
        }
        if !paused && now >= next_frame {
            sequence = sequence.checked_add(1).ok_or("fixture sequence overflow")?;
            capture_ms = now;
            next_frame = now.saturating_add(100);
        }
        let bytes = &images[(sequence % 2) as usize];
        let latest = if paused || sequence == 0 { None } else { Some(Frame {
            sequence,
            capture: Capture { format: Format { width: 320, height: 240, pixels: PixelFormat::Jpeg },
                len: bytes.len(), timestamp_ms: capture_ms },
            bytes,
        }) };
        active = server.poll(now, latest)?.active;
        let stats = server.stats();
        let outstanding = stats.accepted.checked_sub(stats.disconnected).ok_or("invalid server accounting")?;
        if outstanding > 1 || outstanding != u64::from(active) {
            return Err("server admitted more than one outstanding client".into());
        }
        peak = peak.max(outstanding);
        thread::sleep(Duration::from_millis(5));
    }
    input.join().map_err(|_| "fixture input thread failed")?;
    Ok(())
}
