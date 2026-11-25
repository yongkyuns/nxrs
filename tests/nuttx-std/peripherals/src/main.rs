use flash::Partition;
use rtc::Clock;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use watchdog::{ArmedWatchdog, RuntimeWatchdog};

#[cfg(target_os = "nuttx")]
mod io;

fn flash_reset(write: bool) {
    let mut flash = flash::open().expect("open scratch partition");
    let geometry = flash.geometry();
    assert_eq!(geometry.capacity, 64 * 1024);
    assert!(geometry.erase_size <= geometry.capacity / 2);
    assert!(geometry.program_size <= geometry.erase_size);
    let start = geometry.capacity - 2 * geometry.erase_size;
    let pattern: Vec<_> = (0..geometry.program_size)
        .map(|index| (index as u8).wrapping_mul(29).wrapping_add(73))
        .collect();
    let mut observed = vec![0; pattern.len()];
    if write {
        flash.erase(start, 2 * geometry.erase_size).unwrap();
        flash.program(start, &pattern).unwrap();
    }
    flash.read(start, &mut observed).unwrap();
    assert_eq!(observed, pattern);
    flash
        .read(start + geometry.erase_size, &mut observed)
        .unwrap();
    assert!(observed.iter().all(|&byte| byte == 0xff));
    if write {
        drop(flash);
        let _timer = watchdog::arm(Duration::from_secs(3)).expect("arm watchdog");
        println!("FLASH RESET ARMED bytes={}", pattern.len());
        std::thread::sleep(Duration::from_secs(8));
        panic!("watchdog did not reset");
    }
    assert_eq!(boot::reset_cause(), Ok(boot::ResetCause::MainWatchdog));
    flash.erase(start, geometry.erase_size).unwrap();
    println!("FLASH RESET PASS bytes={}", pattern.len());
}

fn main() {
    match std::env::args().nth(1).as_deref() {
        #[cfg(target_os = "nuttx")]
        Some(
            command @ ("io" | "gpio-readback" | "gpio-loop" | "pwm-loop" | "adc" | "adc-scan"),
        ) => {
            io::run(command, std::env::args().skip(2));
            return;
        }
        Some("cause") => {
            println!(
                "RESET CAUSE {:?}",
                boot::reset_cause().expect("reset cause")
            );
            return;
        }
        Some("flash-reset-write") => {
            flash_reset(true);
            return;
        }
        Some("flash-reset-check") => {
            flash_reset(false);
            return;
        }
        Some("expire") => {
            let _timer = watchdog::arm(Duration::from_secs(3)).expect("arm watchdog");
            println!("WATCHDOG EXPIRING");
            std::thread::sleep(Duration::from_secs(8));
            panic!("watchdog did not reset");
        }
        None => {}
        Some(_) => panic!("unknown peripheral qualification command"),
    }
    let mut rtc = rtc::open().expect("RTC");
    let reference = UNIX_EPOCH + Duration::from_secs(1_800_000_000);
    rtc.set(reference).expect("set RTC");
    for observed in [rtc.read().unwrap().unwrap(), SystemTime::now()] {
        assert!(observed.duration_since(reference).unwrap() <= Duration::from_secs(3));
    }
    assert_eq!(
        rtc.set(UNIX_EPOCH - Duration::from_secs(1)),
        Err(rtc::Error::InvalidTime)
    );
    assert_eq!(
        rtc.set(UNIX_EPOCH + Duration::from_secs(4_102_444_800)),
        Err(rtc::Error::InvalidTime)
    );
    println!("RTC PASS");

    let mut timer = watchdog::arm(Duration::from_secs(5)).expect("arm watchdog");
    assert!(matches!(
        watchdog::adopt(),
        Err(watchdog::DeviceError::Unsupported)
    ));
    assert!(matches!(
        watchdog::arm(Duration::from_secs(5)),
        Err(watchdog::DeviceError::Busy)
    ));
    timer.feed().expect("feed watchdog");
    timer.disarm().expect("disarm watchdog");
    assert_eq!(timer.feed(), Err(watchdog::DeviceError::InvalidState));
    println!("WATCHDOG PASS");

    let mut flash = flash::open().expect("open scratch partition");
    assert!(matches!(flash::open(), Err(flash::Error::Busy)));
    let geometry = flash.geometry();
    assert_eq!(geometry.capacity, 64 * 1024);
    assert!(geometry.program_size < geometry.erase_size);
    assert_eq!(
        flash.erase(1, geometry.erase_size),
        Err(flash::Error::Unaligned)
    );
    assert_eq!(
        flash.read(geometry.capacity, &mut [0]),
        Err(flash::Error::OutOfBounds)
    );
    flash.erase(0, 2 * geometry.erase_size).unwrap();
    let data = vec![0x5a; geometry.program_size as usize];
    let mut observed = [0; 3];
    // Prime the proxy's sector cache before programming and again before erasing.
    flash.read(1, &mut observed).unwrap();
    assert_eq!(observed, [0xff; 3]);
    flash.program(geometry.program_size, &data).unwrap();
    flash.program(0, &data).unwrap();
    // A write at the erase boundary must preserve the next program block.
    flash
        .read(geometry.program_size + 1, &mut observed)
        .unwrap();
    assert_eq!(observed, [0x5a; 3]);
    flash.program(geometry.erase_size, &data).unwrap();
    flash.read(1, &mut observed).unwrap();
    assert_eq!(observed, [0x5a; 3]);
    flash.erase(0, geometry.erase_size).unwrap();
    flash.read(1, &mut observed).unwrap();
    assert_eq!(observed, [0xff; 3]);
    flash.read(geometry.erase_size + 1, &mut observed).unwrap();
    assert_eq!(observed, [0x5a; 3]);
    drop(flash);
    let mut flash = flash::open().unwrap();
    flash.read(geometry.erase_size + 1, &mut observed).unwrap();
    assert_eq!(observed, [0x5a; 3]);
    flash
        .erase(geometry.erase_size, geometry.erase_size)
        .unwrap();
    println!("FLASH PASS");
}
