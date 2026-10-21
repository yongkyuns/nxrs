//! Station setup and address policy. Native bindings perform one operation.
#![forbid(unsafe_code)]
use crate::Error;
use std::net::Ipv4Addr;

pub(super) trait Controls {
    fn infra(&mut self) -> Result<(), Error>;
    fn wpa(&mut self, secured: bool) -> Result<(), Error>;
    fn cipher(&mut self, secured: bool) -> Result<(), Error>;
    fn key(&mut self, password: &[u8]) -> Result<(), Error>;
    fn ssid(&mut self, ssid: &[u8]) -> Result<(), Error>;
    fn address(&mut self, field: u32, value: u32) -> Result<(), Error>;
}

pub(super) fn configure(io: &mut impl Controls, ssid: &[u8], password: &[u8]) -> Result<(), Error> {
    if ssid.is_empty()
        || ssid.len() > 32
        || password.len() > 63
        || (!password.is_empty() && password.len() < 8)
    {
        return Err(Error::InvalidConfig);
    }
    io.infra()?;
    io.wpa(!password.is_empty())?;
    io.cipher(!password.is_empty())?;
    io.key(password)?;
    io.ssid(ssid)
}

pub(super) fn clear(io: &mut impl Controls) -> Result<(), Error> {
    // Attempt all three even when one fails. Unknown cleanup is terminal.
    let mut failed = false;
    for field in 0..3 {
        failed |= io.address(field, 0).is_err();
    }
    if failed {
        Err(Error::Io)
    } else {
        Ok(())
    }
}

#[repr(C)]
#[derive(Default, Clone, Copy)]
pub(super) struct Lease {
    pub address: u32,
    pub netmask: u32,
    pub router: u32,
    pub seconds: u32,
}

pub(super) fn install(io: &mut impl Controls, lease: Lease) -> Result<u32, Error> {
    let ip = Ipv4Addr::from(lease.address);
    if ip.is_unspecified()
        || ip.is_broadcast()
        || ip.is_multicast()
        || lease.netmask == 0
        || lease.seconds == 0
    {
        return Err(Error::Protocol);
    }
    io.address(1, lease.netmask)?;
    io.address(2, lease.router)?;
    io.address(0, lease.address)?;
    Ok(lease.seconds)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::vec::Vec;
    #[derive(Default)]
    struct Fake {
        calls: Vec<u32>,
        fail: Option<usize>,
    }
    impl Fake {
        fn call(&mut self, step: u32) -> Result<(), Error> {
            self.calls.push(step);
            if self.fail == Some(self.calls.len()) {
                Err(Error::PermissionDenied)
            } else {
                Ok(())
            }
        }
    }
    impl Controls for Fake {
        fn infra(&mut self) -> Result<(), Error> {
            self.call(0)
        }
        fn wpa(&mut self, _: bool) -> Result<(), Error> {
            self.call(1)
        }
        fn cipher(&mut self, _: bool) -> Result<(), Error> {
            self.call(2)
        }
        fn key(&mut self, _: &[u8]) -> Result<(), Error> {
            self.call(3)
        }
        fn ssid(&mut self, _: &[u8]) -> Result<(), Error> {
            self.call(4)
        }
        fn address(&mut self, field: u32, _: u32) -> Result<(), Error> {
            self.call(field)
        }
    }
    #[test]
    fn configuration_stops_at_every_failed_native_operation() {
        for fail in 1..=5 {
            let mut io = Fake {
                fail: Some(fail),
                ..Fake::default()
            };
            assert_eq!(
                configure(&mut io, b"lab", b"password"),
                Err(Error::PermissionDenied)
            );
            assert_eq!(io.calls, (0..fail as u32).collect::<Vec<_>>());
        }
        let mut io = Fake::default();
        configure(&mut io, b"lab", b"").unwrap();
        assert_eq!(io.calls, [0, 1, 2, 3, 4]);
        io.calls.clear();
        assert_eq!(
            configure(&mut io, b"", b"password"),
            Err(Error::InvalidConfig)
        );
        assert!(io.calls.is_empty());
    }
    #[test]
    fn cleanup_attempts_every_field_and_lease_validation_precedes_mutation() {
        let mut io = Fake {
            fail: Some(1),
            ..Fake::default()
        };
        assert_eq!(clear(&mut io), Err(Error::Io));
        assert_eq!(io.calls, [0, 1, 2]);
        let lease = Lease {
            address: 0x0a000001,
            netmask: 0xffffff00,
            router: 0x0a0000fe,
            seconds: 30,
        };
        for address in [0, u32::MAX, 0xe0000001] {
            let mut io = Fake::default();
            assert_eq!(
                install(&mut io, Lease { address, ..lease }),
                Err(Error::Protocol)
            );
            assert!(io.calls.is_empty());
        }
        let mut io = Fake::default();
        assert_eq!(install(&mut io, lease), Ok(30));
        assert_eq!(io.calls, [1, 2, 0]);
    }
}
