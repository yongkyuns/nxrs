//! Physical TLS composition using the product camera worker and a trusted USB
//! provisioning host. Volatile keys and enrollment last for this boot only.
#[cfg(any(target_os = "nuttx", test))]
#[path = "../tls_console.rs"]
mod console;

#[cfg(target_os = "nuttx")]
fn run() -> Result<(), String> {
    use camera::{Camera, JpegQuality};
    use home_cam::{LiveCamera, TlsOptions};
    use std::net::{Ipv4Addr, TcpListener};
    use std::sync::{
        atomic::{AtomicU8, Ordering},
        Arc,
    };
    use std::time::{Duration, Instant};

    unsafe extern "C" {
        fn nxrs_stream_address(address: *mut u8) -> i32;
        fn nxrs_stream_metrics(values: *mut u32, times: *mut u64) -> i32;
    }
    const BYTES: usize = 320 * 240 * 2;
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    let [seconds] = arguments.as_slice() else {
        return Err("usage: tls_camera SECONDS (10..600)".into());
    };
    let seconds: u64 = seconds.parse().map_err(|_| "invalid duration")?;
    if !(10..=600).contains(&seconds) {
        return Err("duration must be 10..600 seconds".into());
    }
    // Establish the RF entropy source before generating a key. The fixture
    // joins radio callbacks after the TLS and camera owners have stopped.
    wifi_scan_owner::station::with_connection_reactor(|_, network| {
        let mut operation = || -> Result<(), String> {
            let mut address = [0; 4];
            // SAFETY: target-header translation writes exactly four bytes.
            if unsafe { nxrs_stream_address(address.as_mut_ptr()) } != 0 {
                return Err("station address unavailable".into());
            }
            let mut device = camera::open().map_err(|e| format!("camera {e:?}"))?;
            device
                .set_jpeg_quality(JpegQuality::new(81).unwrap())
                .map_err(|e| format!("quality {e:?}"))?;
            let listener = TcpListener::bind("0.0.0.0:8443").map_err(|e| e.to_string())?;
            let stop = Arc::new(AtomicU8::new(0));
            let tls_stop = stop.clone();
            let mut live = LiveCamera::<_, BYTES, _>::new_tls(
                device,
                listener,
                std::array::from_fn(|_| vec![0; BYTES]),
                stream_service::Options::default(),
                TlsOptions {
                    stack_bytes: 64 * 1024,
                    poll_interval: Duration::from_millis(2),
                },
                move || console::initialize(tls_stop),
            )
            .map_err(|e| format!("TLS setup {e:?}"))?;
            let result = (|| {
                live.start(320, 240).map_err(|e| format!("capture {e:?}"))?;
                println!("NXRS_TLS_LISTEN ip={} port=8443", Ipv4Addr::from(address));
                let start = Instant::now();
                let mut next_report = 10_000;
                while stop.load(Ordering::Acquire) == 0 && start.elapsed() < Duration::from_secs(seconds) {
                    network().map_err(|e| format!("network {e:?}"))?;
                    let now = start.elapsed().as_millis() as u64;
                    live.poll(now, true).map_err(|e| format!("stream {e:?}"))?;
                    if now >= next_report {
                        let stats = live.stats();
                        let mut heap = [0; 2];
                        let mut times = [0; 3];
                        // SAFETY: fixed scalar arrays match the target witness.
                        if unsafe { nxrs_stream_metrics(heap.as_mut_ptr(), times.as_mut_ptr()) } != 0 {
                            return Err("resource observation failed".into());
                        }
                        let tls = stats.tls.unwrap();
                        println!("NXRS_TLS_REPORT ms={now} captured={} delivered={} handshakes={} failures={} last={:?} heap_user={} heap_kernel={}",
                            stats.captured, stats.stream.frames_completed, tls.handshakes,
                            tls.failures, tls.last_failure, heap[0], heap[1]);
                        next_report = now + 10_000;
                    }
                    let delay = live.next_poll_at_ms().unwrap_or(now + 10)
                        .saturating_sub(now).clamp(1, 10);
                    std::thread::park_timeout(Duration::from_millis(delay));
                }
                Ok(())
            })();
            let stopped = live.stop().map_err(|e| format!("shutdown {e:?}"));
            result.and(stopped)?;
            let stats = live.stats();
            let tls = stats.tls.unwrap();
            if stats.captured == 0 || stats.stream.frames_completed == 0 || tls.handshakes == 0 {
                return Err("no authenticated camera frames delivered".into());
            }
            println!("NXRS_TLS_DELIVERED captured={} delivered={} handshakes={} failures={}",
                stats.captured, stats.stream.frames_completed, tls.handshakes, tls.failures);
            Ok(())
        };
        operation().map_err(|error| {
            eprintln!("NXRS_TLS_CAMERA_FAIL {error}");
            wifi::Error::Io
        })
    })
    .map_err(|e| format!("station {e:?}"))?;
    println!("NXRS_TLS_CAMERA_PASS stopped=1 joined=1");
    Ok(())
}

fn main() {
    #[cfg(target_os = "nuttx")]
    if let Err(error) = run() {
        eprintln!("NXRS_TLS_CAMERA_FAIL {error}");
        std::process::exit(1);
    }
    #[cfg(not(target_os = "nuttx"))]
    eprintln!("physical NuttX camera required");
}
