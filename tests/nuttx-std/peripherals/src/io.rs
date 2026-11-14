use acquisition::{nuttx, readiness, Wake};
use embedded_hal::{
    digital::{InputPin, OutputPin},
    pwm::SetDutyCycle,
};
use std::{
    fs::File,
    io::{ErrorKind, Read, Seek},
    num::NonZeroU32,
    thread,
    time::{Duration, Instant},
};

fn edges(
    mut irq: gpio::Interrupt<impl Fn() + Send + 'static>,
    count: usize,
    drive: impl FnOnce(),
) -> Vec<Instant> {
    let cancel = readiness::Cancel::new(nuttx::pipe).unwrap();
    let reader = cancel.reader.clone();
    let (send, receive) = std::sync::mpsc::sync_channel(count);
    let worker = thread::spawn(move || {
        for _ in 0..count {
            if !readiness::wait(irq.file(), &reader, nuttx::wait).unwrap() {
                break;
            }
            assert!(irq.read().unwrap());
            if send.send(Instant::now()).is_err() {
                break;
            }
        }
        irq.unregister().unwrap();
    });
    drive();
    let result: Vec<_> = (0..count)
        .map_while(|_| receive.recv_timeout(Duration::from_secs(2)).ok())
        .collect();
    cancel.wake().unwrap();
    worker.join().unwrap();
    assert_eq!(result.len(), count, "missing rising edges");
    result
}

fn measure_duty(level: &mut gpio::Input) -> usize {
    let until = Instant::now() + Duration::from_millis(400);
    let (mut high, mut samples) = (0, 0);
    while Instant::now() < until {
        high += usize::from(level.is_high().unwrap());
        samples += 1;
        thread::sleep(Duration::from_millis(1));
    }
    high * 100 / samples
}

fn read_sample(adc: &mut adc::Adc) -> adc::Sample {
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        match adc.read() {
            Ok(sample) => return sample,
            Err(error) if error.kind() == ErrorKind::WouldBlock && Instant::now() < deadline => {
                thread::sleep(Duration::from_millis(2));
            }
            Err(error) => panic!("ADC read: {error}"),
        }
    }
}

fn expect_empty(adc: &mut adc::Adc) {
    assert!(matches!(adc.read(), Err(error) if error.kind() == ErrorKind::WouldBlock));
}

fn qualify_scans(path: &str, channels: &[u8]) {
    const OPENS: usize = 3;
    const SCANS_PER_OPEN: usize = 32;
    assert!(channels.len() > 1, "expected at least two ADC channels");
    for _ in 0..OPENS {
        let mut adc = adc::Adc::open(path).unwrap();
        expect_empty(&mut adc);
        for _ in 0..SCANS_PER_OPEN {
            adc.trigger().unwrap();
            assert_eq!(read_sample(&mut adc).channel, channels[0]);
            // Leave the rest of the previous scan queued. A fresh scan must
            // start at the first channel again and contain exactly one record per channel.
            adc.trigger().unwrap();
            for &channel in channels {
                assert_eq!(read_sample(&mut adc).channel, channel);
            }
            expect_empty(&mut adc);
        }
    }
    println!(
        "ADC SCAN PASS triggers={} complete_scans={} opens={OPENS}",
        OPENS * SCANS_PER_OPEN * 2,
        OPENS * SCANS_PER_OPEN
    );
}

pub fn run(command: &str, mut args: impl Iterator<Item = String>) {
    let path = args.next().expect("device path");
    match command {
        "io" => {
            let mut pin = gpio::Output::open(&path).unwrap();
            pin.set_high().unwrap();
            pin.set_low().unwrap();
            let pwm = args.next().expect("PWM path");
            let mut output = pwm::Output::start(&pwm, NonZeroU32::new(25).unwrap(), 32768).unwrap();
            output.set_duty_cycle(16384).unwrap();
            output.stop().unwrap();
            println!("GPIO PWM CONTROL PASS");
        }
        "gpio-readback" => {
            const OPENS: usize = 4;
            const CYCLES_PER_OPEN: usize = 16;
            for _ in 0..OPENS {
                let mut output = gpio::Output::open(&path).unwrap();
                // This BSP's output device reads the pad through its enabled input buffer.
                let mut pad = File::open(&path).unwrap();
                let mut level = [0];
                for _ in 0..CYCLES_PER_OPEN {
                    for expected in [b'1', b'0'] {
                        output.set_state((expected == b'1').into()).unwrap();
                        pad.rewind().unwrap();
                        pad.read_exact(&mut level).unwrap();
                        assert_eq!(level, [expected], "GPIO pad did not follow output");
                    }
                }
            }
            println!(
                "GPIO READBACK PASS levels={} opens={OPENS}",
                OPENS * CYCLES_PER_OPEN * 2
            );
        }
        "gpio-loop" => {
            let mut pin = gpio::Output::open(&path).unwrap();
            let input = args.next().expect("IRQ path");
            let mut level = gpio::Input::open(&input).unwrap();
            pin.set_low().unwrap();
            assert!(level.is_low().unwrap());
            let irq = gpio::Interrupt::open_rising(&input, || {}).unwrap();
            edges(irq, 1, || pin.set_high().unwrap());
            assert!(level.is_high().unwrap());
            pin.set_low().unwrap();
            println!("GPIO EDGE PASS");
        }
        "pwm-loop" => {
            let input = args.next().expect("IRQ path");
            let mut level = gpio::Input::open(&input).unwrap();
            let irq = gpio::Interrupt::open_rising(&input, || {}).unwrap();
            let mut pwm = None;
            let observed = edges(irq, 6, || {
                pwm = Some(pwm::Output::start(&path, NonZeroU32::new(25).unwrap(), 16384).unwrap());
            });
            let mut pwm = pwm.unwrap();
            let (mut min_ms, mut max_ms) = (u128::MAX, 0);
            for pair in observed.windows(2) {
                let ms = pair[1].duration_since(pair[0]).as_millis();
                min_ms = min_ms.min(ms);
                max_ms = max_ms.max(ms);
            }
            assert!(min_ms >= 35 && max_ms <= 45, "period {min_ms}–{max_ms} ms");
            let quarter = measure_duty(&mut level);
            pwm.set_duty_cycle(32768).unwrap();
            let half = measure_duty(&mut level);
            pwm.stop().unwrap();
            assert!((15..=35).contains(&quarter), "observed duty {quarter}%");
            assert!((40..=60).contains(&half), "observed duty {half}%");
            let idle = level.is_high().unwrap();
            let until = Instant::now() + Duration::from_millis(80);
            while Instant::now() < until {
                assert_eq!(level.is_high().unwrap(), idle, "PWM still toggling");
                thread::sleep(Duration::from_millis(1));
            }
            println!(
                "PWM LOOP PASS edges=6 period_ms={min_ms}..{max_ms} duty_percent={quarter},{half}"
            );
        }
        "adc" => {
            let mut adc = adc::Adc::open(&path).unwrap();
            if args.next().as_deref() == Some("trigger") {
                adc.trigger().unwrap();
            }
            let sample = read_sample(&mut adc);
            println!(
                "ADC SAMPLE channel={} value={}",
                sample.channel, sample.value
            );
        }
        "adc-scan" => {
            let channels: Vec<_> = args.map(|channel| channel.parse().unwrap()).collect();
            qualify_scans(&path, &channels);
        }
        _ => unreachable!(),
    }
}
