#![cfg_attr(target_os = "none", no_std)]
#![cfg_attr(target_os = "none", no_main)]
#![cfg_attr(target_os = "none", feature(asm_experimental_arch))]

// Integration tests build binaries too; firmware dependencies are target-only.
#[cfg(not(target_os = "none"))]
fn main() {}

#[cfg(target_os = "none")]
mod board {
    use core::fmt::Write;
    use esp_hal::{clock::CpuClock, time::Instant, usb_serial_jtag::UsbSerialJtag};

    esp_bootloader_esp_idf::esp_app_desc!();

    #[cfg(not(feature = "baseline"))]
    mod workload {
        use core::{
            future::poll_fn,
            sync::atomic::{AtomicU32, Ordering},
            task::Poll,
        };
        use embassy_executor::{Spawner, raw::Executor};
        use embassy_sync::{
            blocking_mutex::raw::CriticalSectionRawMutex, channel::Channel, signal::Signal,
        };
        use nxrs_embassy_comparison::messaging::receive_any;
        use nxrs_embassy_comparison::protocol::*;
        use static_cell::StaticCell;

        type Input = Channel<CriticalSectionRawMutex, Event, 1>;
        type Output = Channel<CriticalSectionRawMutex, Ack, 4>;
        static INPUTS: [Input; LANES] = [const { Input::new() }; LANES];
        static OUTPUTS: [Output; WORKERS] = [const { Output::new() }; WORKERS];
        static GATES: [Signal<CriticalSectionRawMutex, ()>; 20] = [const { Signal::new() }; 20];
        static RESULT: Signal<CriticalSectionRawMutex, Summary> = Signal::new();
        static READY: AtomicU32 = AtomicU32::new(0);
        static DONE: AtomicU32 = AtomicU32::new(0);
        static END: AtomicU32 = AtomicU32::new(0);
        static EXECUTOR: StaticCell<Executor> = StaticCell::new();

        #[inline]
        fn cycles() -> u32 {
            let result: u32;
            // Read the single-core CPU cycle counter used by the C/NuttX tests.
            unsafe {
                core::arch::asm!("rsr.ccount {0}", out(reg) result, options(nomem, nostack));
            }
            result
        }

        async fn ready(id: usize) {
            READY.fetch_add(1, Ordering::Release);
            GATES[id].wait().await;
        }

        // Separate scheduling control: bound a role's uninterrupted work to
        // one event/reply. Primary builds yield only when queue operations wait.
        async fn scheduling_point() {
            #[cfg(feature = "cooperative-yield")]
            {
                let mut yielded = false;
                poll_fn(|cx| {
                    if yielded {
                        Poll::Ready(())
                    } else {
                        yielded = true;
                        cx.waker().wake_by_ref();
                        Poll::Pending
                    }
                })
                .await;
            }
        }

        #[embassy_executor::task(pool_size = 4)]
        async fn producer(id: usize) {
            ready(id).await;
            for sequence in 0..SEQUENCES {
                for lane in 0..LANES {
                    let mut event = Event::new(lane, id, sequence).unwrap();
                    event.stamp(cycles());
                    INPUTS[lane].send(event).await;
                    scheduling_point().await;
                }
            }
            DONE.fetch_add(1, Ordering::Release);
        }

        #[embassy_executor::task(pool_size = 15)]
        async fn worker(id: usize) {
            let mut state = WorkerState::new(id);
            let mut cursor = 0;
            let queues = &INPUTS[id * LANES_PER_WORKER..(id + 1) * LANES_PER_WORKER];
            ready(PRODUCERS + id).await;
            for _ in 0..LANES_PER_WORKER * PRODUCERS * SEQUENCES as usize {
                let (queue, event) = receive_any(queues, &mut cursor).await;
                assert_eq!(event.lane as usize, id * LANES_PER_WORKER + queue);
                let validated = state.validate(&event).unwrap();
                let ack = validated.acknowledge(cycles());
                OUTPUTS[id].send(ack).await;
                scheduling_point().await;
            }
            assert!(state.is_complete());
            DONE.fetch_add(1, Ordering::Release);
        }

        #[embassy_executor::task]
        async fn collector() {
            let mut state = CollectorState::new();
            let mut cursor = 0;
            ready(PRODUCERS + WORKERS).await;
            for index in 0..LANES * PRODUCERS * SEQUENCES as usize {
                let (worker, ack) = receive_any(&OUTPUTS, &mut cursor).await;
                let collected = cycles();
                state.accept(worker, &ack, collected).unwrap();
                // Do not include a trailing yield in the last-reply end time.
                if index + 1 < LANES * PRODUCERS * SEQUENCES as usize {
                    scheduling_point().await;
                }
            }
            // End before sorting samples and printing, as in the RTOS tests.
            END.store(cycles(), Ordering::Release);
            let summary = state.finish().unwrap();
            assert_eq!(summary.digest, EXPECTED_DIGEST);
            RESULT.signal(summary);
            DONE.fetch_add(1, Ordering::Release);
        }

        fn spawn(spawner: Spawner) {
            for id in 0..PRODUCERS {
                spawner.spawn(producer(id).unwrap());
            }
            for id in 0..WORKERS {
                spawner.spawn(worker(id).unwrap());
            }
            spawner.spawn(collector().unwrap());
        }

        pub fn executor() -> &'static Executor {
            EXECUTOR.init(Executor::new(core::ptr::null_mut()))
        }

        pub fn run(executor: &'static Executor) -> (u32, Summary) {
            assert!(INPUTS.iter().all(Input::is_empty));
            assert!(OUTPUTS.iter().all(Output::is_empty));
            READY.store(0, Ordering::Release);
            DONE.store(0, Ordering::Release);
            RESULT.reset();
            for gate in &GATES {
                gate.reset();
            }
            spawn(executor.spawner());
            let deadline = super::Instant::now();
            while READY.load(Ordering::Acquire) != 20 {
                // Only this coordinator polls; never recursively or in an ISR.
                unsafe {
                    executor.poll();
                }
                assert!(deadline.elapsed().as_millis() < 16_000);
            }
            let start = cycles();
            for gate in &GATES {
                gate.signal(());
            }
            while DONE.load(Ordering::Acquire) != 20 {
                unsafe {
                    executor.poll();
                }
                assert!(deadline.elapsed().as_millis() < 16_000);
            }
            assert!(INPUTS.iter().all(Input::is_empty));
            assert!(OUTPUTS.iter().all(Output::is_empty));
            let elapsed = END.load(Ordering::Acquire).wrapping_sub(start);
            assert!(elapsed > 0 && elapsed < 3_840_000_000);
            (elapsed, RESULT.try_take().unwrap())
        }

        pub fn resources(console: &mut impl core::fmt::Write) {
            use core::mem::size_of;
            writeln!(console, "EMBASSY_RESOURCES heap_allocated=0 queue_buffers={} channel_storage={} shared_stack=8192 tasks=20 queues=60",
                LANES * size_of::<Event>() + WORKERS * 4 * size_of::<Ack>(),
                size_of::<[Input; LANES]>() + size_of::<[Output; WORKERS]>()).unwrap();
        }
    }

    #[esp_hal::main]
    fn main() -> ! {
        let peripherals = esp_hal::init(esp_hal::Config::default().with_cpu_clock(CpuClock::max()));
        let mut console = UsbSerialJtag::new(peripherals.USB_DEVICE);
        #[cfg(not(feature = "baseline"))]
        let executor = workload::executor();
        writeln!(console, "EMBASSY_READY cpu_mhz=240").unwrap();
        write!(console, "embassy> ").unwrap();
        let mut line = [0u8; 16];
        let mut used = 0;
        loop {
            if let Ok(byte) = console.read_byte() {
                if byte == b'\r' || byte == b'\n' {
                    if used == 0 {
                        write!(console, "embassy> ").unwrap();
                        continue;
                    }
                    let command = &line[..used];
                    match command {
                        b"baseline" => {
                            writeln!(
                                console,
                                "EMBASSY_BASELINE_PASS heap_allocated=0 shared_stack=8192"
                            )
                            .unwrap();
                        }
                        #[cfg(not(feature = "baseline"))]
                        b"large" => {
                            let (cycles, summary) = workload::run(executor);
                            workload::resources(&mut console);
                            writeln!(console, "CQ_TRANSPORT_PASS language=embassy mode=large cycles={} event_bytes=248 ack_bytes={}",
                                cycles, core::mem::size_of::<nxrs_embassy_comparison::protocol::Ack>()).unwrap();
                            writeln!(console, "CQ_EMBASSY_SCALE_PASS mode=large queues=60 logical_streams=60 tasks=20 messages={} digest={} rx_p50_us={} rx_p99_us={} rx_max_us={} ack_p50_us={} ack_p99_us={} ack_max_us={} samples={}",
                                summary.received, summary.digest, summary.inbound_p50_us, summary.inbound_p99_us,
                                summary.inbound_max_us, summary.outbound_p50_us, summary.outbound_p99_us,
                                summary.outbound_max_us, summary.samples).unwrap();
                        }
                        _ => {
                            writeln!(console, "EMBASSY_COMMAND_EXIT status=1").unwrap();
                            used = 0;
                            write!(console, "embassy> ").unwrap();
                            continue;
                        }
                    }
                    writeln!(console, "EMBASSY_COMMAND_EXIT status=0").unwrap();
                    used = 0;
                    write!(console, "embassy> ").unwrap();
                } else if used < line.len() {
                    line[used] = byte;
                    used += 1;
                }
            }
        }
    }

    #[panic_handler]
    fn panic(info: &core::panic::PanicInfo) -> ! {
        // Fatal benchmark errors never resume/reuse task pools. USB output lets
        // the harness reject failure rather than accepting a truncated result.
        let mut console = UsbSerialJtag::new(unsafe { esp_hal::peripherals::USB_DEVICE::steal() });
        writeln!(console, "EMBASSY_FAIL {}", info).ok();
        loop {
            core::hint::spin_loop();
        }
    }
}
