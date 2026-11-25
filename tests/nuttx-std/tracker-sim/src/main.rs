use cellular::{Error, Session, Status};
use std::{
    io,
    net::UdpSocket,
    time::{Duration, Instant},
};

use battery::Device as _;
use display::Display as _;
use gnss::{Device as _, Session as _};
use imu::{Device as _, Session as _};
use settings::Store as _;
use touch::Session as _;

unsafe extern "C" {
    fn tracker_sim_devices() -> i32;
    fn tracker_sim_touch();
    fn tracker_sim_pixel() -> u16;
}

fn error(value: impl std::fmt::Debug) -> io::Error {
    io::Error::other(format!("{value:?}"))
}

fn devices() -> io::Result<()> {
    // SAFETY: the fixture registers its static models exactly once at startup.
    assert_eq!(unsafe { tracker_sim_devices() }, 0);
    println!("TRACKER_DEVICE registered");
    let status = composition::battery::primary_battery()
        .status()
        .map_err(error)?;
    assert!(status.external_power);
    assert_eq!(status.battery.unwrap().percent, Some(73));

    println!("TRACKER_DEVICE battery");
    let mut panel = display::open().map_err(error)?;
    panel
        .draw(
            display::Rect {
                x: 12,
                y: 34,
                size: display::Size {
                    width: 1,
                    height: 1,
                },
            },
            &[0xf81f],
        )
        .map_err(error)?;
    // SAFETY: the draw finished synchronously and the fixture owns the model.
    assert_eq!(unsafe { tracker_sim_pixel() }, 0xf81f);
    drop(panel);

    println!("TRACKER_DEVICE display");
    settings::open()
        .map_err(error)?
        .set("sim.alpha", b"tracker")
        .map_err(error)?;
    let mut stored = [0; 7];
    assert_eq!(
        settings::open()
            .map_err(error)?
            .get("sim.alpha", &mut stored)
            .map_err(error)?,
        Some(7)
    );
    assert_eq!(&stored, b"tracker");

    println!("TRACKER_DEVICE settings");
    let (send_touch, recv_touch) = std::sync::mpsc::sync_channel(4);
    let mut touchscreen = touch::open(move |event| {
        let _ = send_touch.try_send(event);
        true
    })
    .map_err(error)?;
    // SAFETY: the fixture sets simulated touch input under its I2C lock.
    unsafe { tracker_sim_touch() };
    let start = Instant::now();
    loop {
        if let touch::Event::Pressed(point) = recv_touch
            .recv_timeout(Duration::from_secs(2))
            .map_err(error)?
        {
            assert_eq!(point, touch::Point { x: 87, y: 112 });
            break;
        }
        assert!(start.elapsed() < Duration::from_secs(2));
    }
    touchscreen.request_stop().map_err(error)?;
    touchscreen.join().map_err(error)?;

    println!("TRACKER_DEVICE touch");
    let (first_tx, first_rx) = std::sync::mpsc::sync_channel(2);
    let (second_tx, second_rx) = std::sync::mpsc::sync_channel(2);
    let mut first = composition::imu::primary_imu()
        .open_default(events(first_tx))
        .map_err(error)?;
    let mut second = composition::imu::secondary()
        .open_default(events(second_tx))
        .map_err(error)?;
    for (receiver, positive) in [(first_rx, false), (second_rx, true)] {
        match receiver
            .recv_timeout(Duration::from_secs(2))
            .map_err(error)?
        {
            imu::Event::Sample(sample) => {
                assert_eq!(sample.accel_mps2[0].is_sign_positive(), positive)
            }
            event => return Err(error(event)),
        }
    }
    first.request_stop().map_err(error)?;
    second.request_stop().map_err(error)?;
    first.join().map_err(error)?;
    second.join().map_err(error)?;

    println!("TRACKER_DEVICE imus");
    let (fix_tx, fix_rx) = std::sync::mpsc::sync_channel(2);
    let mut gnss = composition::gnss::primary_gnss()
        .open(events(fix_tx))
        .map_err(error)?;
    println!("TRACKER_GNSS_READY");
    match fix_rx.recv_timeout(Duration::from_secs(4)).map_err(error)? {
        gnss::Event::Fix(fix) => assert!((fix.latitude_deg - 48.1173).abs() < 1e-5),
        event => return Err(error(event)),
    }
    gnss.request_stop().map_err(error)?;
    gnss.join().map_err(error)?;
    println!("TRACKER_DEVICES PASS");
    Ok(())
}

fn events<E: Send>(
    tx: std::sync::mpsc::SyncSender<E>,
) -> impl FnMut(E) -> messaging::DeliveryResult<E> + Send {
    move |event| match tx.try_send(event) {
        Ok(()) => Ok(()),
        Err(std::sync::mpsc::TrySendError::Full(event)) => Err(messaging::Rejected {
            value: event,
            error: messaging::DeliveryError::Full,
        }),
        Err(std::sync::mpsc::TrySendError::Disconnected(event)) => Err(messaging::Rejected {
            value: event,
            error: messaging::DeliveryError::Closed,
        }),
    }
}

fn wait_for(link: &impl Session, expected: Status) -> io::Result<()> {
    let until = Instant::now() + Duration::from_secs(20);
    loop {
        let actual = link.status();
        if actual == expected {
            return Ok(());
        }
        if matches!(actual, Status::Failed(_) | Status::Stopped) || Instant::now() >= until {
            return Err(io::Error::other(format!(
                "cellular status {actual:?}, expected {expected:?}"
            )));
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

fn main() -> io::Result<()> {
    let start = Instant::now();
    std::thread::sleep(Duration::from_secs(10));
    assert!(start.elapsed() >= Duration::from_secs(10));
    println!("CELLULAR_CLOCK {}", start.elapsed().as_millis());
    devices()?;
    let mut rejected = cellular::connect("nxrs.test").map_err(io::Error::other)?;
    wait_for(&rejected, Status::Failed(Error::Rejected))?;
    assert_eq!(rejected.stop(), Err(Error::Rejected));
    assert_eq!(rejected.status(), Status::Failed(Error::Rejected));
    println!("CELLULAR REJECTED PASS");

    for cycle in 1..=2 {
        let mut link = cellular::connect("nxrs.test").map_err(io::Error::other)?;
        assert!(matches!(cellular::connect("nxrs.test"), Err(Error::Busy)));
        wait_for(
            &link,
            Status::Online {
                address: std::net::Ipv4Addr::new(192, 0, 2, 2),
            },
        )?;
        let packet = [b'n', b'x', b'r', b's', cycle];
        let udp = UdpSocket::bind((std::net::Ipv4Addr::UNSPECIFIED, 0))?;
        udp.set_read_timeout(Some(Duration::from_secs(3)))?;
        udp.send_to(&packet, (std::net::Ipv4Addr::new(192, 0, 2, 1), 9090))?;
        let mut returned = [0; 16];
        let (len, source) = udp.recv_from(&mut returned)?;
        assert_eq!(&returned[..len], &packet);
        assert_eq!(source.to_string(), "192.0.2.1:9090");
        println!("CELLULAR_UDP {cycle} PASS");
        if cycle == 1 {
            // The host PPP peer terminates the established link at this point.
            wait_for(&link, Status::Failed(Error::LinkLost))?;
            assert_eq!(link.stop(), Err(Error::LinkLost));
            assert_eq!(link.status(), Status::Failed(Error::LinkLost));
            println!("CELLULAR LINK LOST PASS");
        } else {
            link.stop().map_err(io::Error::other)?;
            assert_eq!(link.status(), Status::Stopped);
        }
    }
    Ok(())
}
