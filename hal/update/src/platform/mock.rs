//! Explicit NOR model. It is not a physical flash or protected-state provider.
#![forbid(unsafe_code)]
use crate::{
    controller::Stager,
    worker::{CryptoVerifier, Flash},
    Error, Policy,
};
use std::{time::Duration, vec, vec::Vec};
struct Memory {
    primary: Vec<u8>,
    secondary: Vec<u8>,
    layout: crate::Layout,
}
impl Flash for Memory {
    fn validate(&mut self, policy: &Policy) -> Result<(), Error> {
        if policy.layout == self.layout {
            Ok(())
        } else {
            Err(Error::Geometry)
        }
    }
    fn read_primary(&mut self, offset: u32, bytes: &mut [u8]) -> Result<(), Error> {
        bytes.copy_from_slice(
            self.primary
                .get(offset as usize..offset as usize + bytes.len())
                .ok_or(Error::Geometry)?,
        );
        Ok(())
    }
    fn read_secondary(&mut self, offset: u32, bytes: &mut [u8]) -> Result<(), Error> {
        bytes.copy_from_slice(
            self.secondary
                .get(offset as usize..offset as usize + bytes.len())
                .ok_or(Error::Geometry)?,
        );
        Ok(())
    }
    fn erase_secondary(&mut self, block: u32) -> Result<(), Error> {
        let start = block as usize * self.layout.erase_bytes as usize;
        self.secondary
            .get_mut(start..start + self.layout.erase_bytes as usize)
            .ok_or(Error::Geometry)?
            .fill(255);
        Ok(())
    }
    fn write_secondary(&mut self, offset: u32, bytes: &[u8]) -> Result<(), Error> {
        let target = self
            .secondary
            .get_mut(offset as usize..offset as usize + bytes.len())
            .ok_or(Error::Geometry)?;
        if target.iter().zip(bytes).any(|(old, new)| old & new != *new) {
            return Err(Error::Io);
        }
        target.copy_from_slice(bytes);
        Ok(())
    }
    fn close(&mut self) -> Result<(), Error> {
        Ok(())
    }
}
pub fn open<C: crate::Crypto + Send + 'static>(
    policy: Policy,
    crypto: C,
    retirement_timeout: Duration,
    wake: impl Fn() + Send + Sync + 'static,
) -> Result<Stager, Error> {
    let bytes = policy.layout.slot_bytes as usize;
    let memory = Memory {
        primary: vec![255; bytes],
        secondary: vec![255; bytes],
        layout: policy.layout,
    };
    Stager::spawn(
        memory,
        CryptoVerifier(crypto),
        policy,
        256 * 1024,
        retirement_timeout,
        wake,
    )
}
