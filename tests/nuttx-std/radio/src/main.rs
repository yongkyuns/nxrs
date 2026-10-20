//! Optional portable-client and native-peripheral radio qualification.
#![deny(unsafe_op_in_unsafe_fn)]

#[cfg(target_os = "nuttx")]
mod device;

#[cfg(any(target_os = "nuttx", test))]
mod echo;

#[cfg(any(target_os = "nuttx", test))]
mod client;

#[cfg(any(target_os = "nuttx", test))]
const CONTROL_PACKETS: u32 = 1000;

#[cfg(any(target_os = "nuttx", test))]
const GATT_ROUNDS: u32 = 100;

#[cfg(any(target_os = "nuttx", test))]
fn gatt_complete(
    writes: u32,
    reads: u32,
    invalid: u32,
    peer_lost: bool,
    traffic_complete: bool,
) -> Result<bool, wifi::Error> {
    if invalid != 0 || writes > GATT_ROUNDS {
        return Err(wifi::Error::Protocol);
    }
    if writes == GATT_ROUNDS && reads >= GATT_ROUNDS && traffic_complete {
        return Ok(true);
    }
    if peer_lost {
        return Err(wifi::Error::Io);
    }
    Ok(false)
}

#[cfg(any(target_os = "nuttx", test))]
fn cancellation_complete(
    writes: u32,
    reads: u32,
    invalid: u32,
    connected: u32,
    disconnected: u32,
    native_connected: u32,
) -> Result<bool, wifi::Error> {
    if writes != 0
        || reads != 0
        || invalid != 0
        || connected > 1
        || disconnected > connected
        || native_connected > 1
    {
        return Err(wifi::Error::Protocol);
    }
    Ok(connected == 1 && disconnected == 1 && native_connected == 0)
}

#[cfg(any(target_os = "nuttx", test))]
#[derive(Clone, Copy, PartialEq, Eq)]
enum RunMode {
    Qualification,
    WifiOnly,
    CoexistControl,
    PortableClient,
}

#[cfg(any(target_os = "nuttx", test))]
impl RunMode {
    fn parse(name: Option<&str>) -> Option<Self> {
        match name {
            None => Some(Self::Qualification),
            Some("wifi-only") => Some(Self::WifiOnly),
            Some("coexist-control") => Some(Self::CoexistControl),
            Some("portable-client") => Some(Self::PortableClient),
            _ => None,
        }
    }

    fn packet_limit(self) -> Option<u32> {
        match self {
            Self::Qualification | Self::PortableClient => None,
            Self::WifiOnly | Self::CoexistControl => Some(CONTROL_PACKETS),
        }
    }

    fn starts_immediately(self) -> bool {
        matches!(self, Self::Qualification | Self::WifiOnly)
    }
}

#[cfg(any(target_os = "nuttx", test))]
fn finish_gatt(
    observed: Result<bool, wifi::Error>,
    disconnect: impl FnOnce() -> Result<(), wifi::Error>,
) -> Result<(), wifi::Error> {
    let overlapped = observed?;
    // All GATT exchanges completed, not necessarily the coexistence test.
    // Preserve the failure, but wait for disconnect before native teardown.
    disconnect()?;
    if overlapped {
        Ok(())
    } else {
        Err(wifi::Error::Protocol)
    }
}

#[cfg(target_os = "nuttx")]
fn main() {
    let result = device::run();
    if let Err(error) = result {
        eprintln!("NXRS_RADIO_FAIL error={error:?}");
        std::process::exit(1);
    }
}

#[cfg(not(target_os = "nuttx"))]
fn main() {
    eprintln!("radio_test requires a NuttX radio platform");
    std::process::exit(2);
}

#[cfg(test)]
mod tests {
    use super::finish_gatt;
    use wifi::Error;

    #[test]
    fn cancellation_requires_one_retired_link_and_no_application_traffic() {
        for (observed, expected) in [
            ([0, 0, 0, 0, 0, 0], Ok(false)),
            ([0, 0, 0, 1, 0, 1], Ok(false)),
            ([0, 0, 0, 1, 1, 1], Ok(false)),
            ([0, 0, 0, 1, 1, 0], Ok(true)),
            ([0, 0, 0, 0, 1, 0], Err(Error::Protocol)),
            ([0, 0, 0, 2, 2, 0], Err(Error::Protocol)),
            ([1, 0, 0, 1, 1, 0], Err(Error::Protocol)),
            ([0, 1, 0, 1, 1, 0], Err(Error::Protocol)),
            ([0, 0, 1, 1, 1, 0], Err(Error::Protocol)),
        ] {
            let [writes, reads, invalid, connected, disconnected, native] = observed;
            assert_eq!(
                super::cancellation_complete(
                    writes,
                    reads,
                    invalid,
                    connected,
                    disconnected,
                    native
                ),
                expected
            );
        }
    }

    #[test]
    fn peer_loss_is_terminal_unless_the_required_workload_already_completed() {
        for (writes, reads, invalid, lost, traffic, expected) in [
            (20, 20, 0, true, false, Err(Error::Io)),
            (100, 100, 0, true, true, Ok(true)),
            (100, 100, 0, true, false, Err(Error::Io)),
            (100, 99, 0, true, true, Err(Error::Io)),
            (20, 20, 0, false, false, Ok(false)),
            (100, 100, 0, false, false, Ok(false)),
            (100, 100, 1, true, true, Err(Error::Protocol)),
            (101, 100, 0, true, true, Err(Error::Protocol)),
        ] {
            assert_eq!(
                super::gatt_complete(writes, reads, invalid, lost, traffic),
                expected
            );
        }
    }

    #[test]
    fn study_modes_are_explicit_and_share_a_fixed_packet_limit() {
        use super::{RunMode, CONTROL_PACKETS};
        assert!(matches!(RunMode::parse(None), Some(RunMode::Qualification)));
        assert!(matches!(
            RunMode::parse(Some("wifi-only")),
            Some(RunMode::WifiOnly)
        ));
        assert!(matches!(
            RunMode::parse(Some("coexist-control")),
            Some(RunMode::CoexistControl)
        ));
        for invalid in ["", "coexist", "other", "1000"] {
            assert!(RunMode::parse(Some(invalid)).is_none());
        }
        assert_eq!(RunMode::Qualification.packet_limit(), None);
        assert!(matches!(
            RunMode::parse(Some("portable-client")),
            Some(RunMode::PortableClient)
        ));
        assert_eq!(RunMode::PortableClient.packet_limit(), None);
        assert!(RunMode::Qualification.starts_immediately());
        assert!(RunMode::WifiOnly.starts_immediately());
        assert!(!RunMode::CoexistControl.starts_immediately());
        assert!(!RunMode::PortableClient.starts_immediately());
        assert_eq!(RunMode::WifiOnly.packet_limit(), Some(CONTROL_PACKETS));
        assert_eq!(
            RunMode::CoexistControl.packet_limit(),
            Some(CONTROL_PACKETS)
        );
    }

    #[test]
    fn completed_gatt_disconnects_even_without_network_overlap() {
        for overlapped in [false, true] {
            let mut disconnected = false;
            let result = finish_gatt(Ok(overlapped), || {
                disconnected = true;
                Ok(())
            });
            assert!(disconnected);
            assert_eq!(
                result,
                if overlapped {
                    Ok(())
                } else {
                    Err(Error::Protocol)
                }
            );
        }
    }

    #[test]
    fn failed_disconnect_cannot_pass_or_be_hidden_by_missing_overlap() {
        for overlapped in [false, true] {
            assert_eq!(
                finish_gatt(Ok(overlapped), || Err(Error::Timeout)),
                Err(Error::Timeout)
            );
        }
    }

    #[test]
    fn incomplete_gatt_does_not_report_completed_exchanges() {
        assert_eq!(
            finish_gatt(Err(Error::Protocol), || panic!("GATT was not complete")),
            Err(Error::Protocol)
        );
    }
}
