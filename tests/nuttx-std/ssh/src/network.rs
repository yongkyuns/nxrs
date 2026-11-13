//! Compose the production reconnect policy with the SSH qualification console.
use connectivity_service::Reconnector;
use std::{
    io,
    sync::mpsc::{sync_channel, TryRecvError, TrySendError},
    time::{Duration, Instant},
};
use wifi::{Command, Credentials, DeliveryError, LinkState, Passphrase, ProviderStatus, Rejected};

pub(super) struct State {
    pub addressed: bool,
    pub link_losses: u64,
}

pub(super) fn with_station<T>(
    run: impl FnOnce(&mut dyn FnMut(bool) -> io::Result<State>) -> io::Result<T>,
) -> io::Result<T> {
    let (ssid, credentials) = wifi_scan_owner::station::credentials().map_err(error)?;
    let (sender, events) = sync_channel(8);
    let controller = wifi::open(move |event| {
        sender.try_send(event).map_err(|error| match error {
            TrySendError::Full(value) => Rejected {
                value,
                error: DeliveryError::Full,
            },
            TrySendError::Disconnected(value) => Rejected {
                value,
                error: DeliveryError::Closed,
            },
        })
    })
    .map_err(error)?;
    let mut network = Reconnector::new(controller, || {
        let credentials = match &credentials {
            Credentials::Open => Credentials::Open,
            Credentials::Wpa2Personal(value) => Credentials::Wpa2Personal(Passphrase::new(
                std::str::from_utf8(value.as_bytes()).map_err(|_| wifi::Error::InvalidConfig)?,
            )?),
        };
        Ok(Command::Connect { ssid, credentials })
    });
    let origin = Instant::now();
    let now = || origin.elapsed().as_millis() as u64;
    let poll = |network: &mut Reconnector<_, _>| {
        let elapsed = now();
        let mut delivery = Ok(());
        for _ in 0..16 {
            match events.try_recv() {
                Ok(event) => {
                    delivery = delivery.and(network.on_event(event, elapsed).map_err(error))
                }
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => {
                    if network.status() != ProviderStatus::Stopped {
                        delivery = delivery.and(Err(error(wifi::Error::Unavailable)));
                    }
                    break;
                }
            }
        }
        let advanced = network.poll(elapsed).map_err(error);
        delivery.and(advanced)?;
        Ok(State {
            addressed: network.link() == LinkState::Addressed,
            link_losses: network.stats().link_losses,
        })
    };
    let result = (|| {
        while !poll(&mut network)?.addressed {
            if origin.elapsed() >= Duration::from_secs(30) {
                return Err(io::ErrorKind::TimedOut.into());
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        println!("NXRS_WIFI_ASSOCIATED_DHCP");
        run(&mut |reconnect| {
            if reconnect {
                network.request_reconnect(now()).map_err(error)?;
            }
            poll(&mut network)
        })
    })();
    let stopping = network.request_disconnect_and_stop(now()).map_err(error);
    let deadline = Instant::now() + Duration::from_secs(35);
    while network.status() != ProviderStatus::Stopped && Instant::now() < deadline {
        // A retained error must not prevent shutdown and join.
        let _ = poll(&mut network);
        std::thread::sleep(Duration::from_millis(20));
    }
    let joined = network.finish().map_err(error);
    result.and_then(|value| stopping.and(joined).map(|()| value))
}

fn error(value: impl std::fmt::Debug) -> io::Error {
    io::Error::other(format!("Wi-Fi owner: {value:?}"))
}
