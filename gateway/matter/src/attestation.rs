//! Operator-provisioned device attestation inputs and their durable identity.
use crate::{persistence::Store, platform};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{io::Read, path::Path};
use zeroize::Zeroizing;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Error {
    Invalid,
    Unreadable,
    IdentityMismatch,
    Storage,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[non_exhaustive]
pub struct ProductIdentity {
    version: u8,
    pub vendor_id: u16,
    pub product_id: u16,
    pub vendor_name: String,
    pub product_name: String,
    pub serial_number: String,
}

pub struct Attestation {
    identity: ProductIdentity,
    dac: Vec<u8>,
    pai: Vec<u8>,
    declaration: Vec<u8>,
    key: Zeroizing<[u8; 32]>,
}

impl Attestation {
    pub fn load(directory: &Path) -> Result<Self, Error> {
        if !directory.is_absolute() {
            return Err(Error::Invalid);
        }
        let opened = platform::open_directory(directory).map_err(|_| Error::Unreadable)?;
        let identity: ProductIdentity =
            serde_json::from_slice(&read_bounded(&directory.join("device.json"), 1024)?)
                .map_err(|_| Error::Invalid)?;
        if identity.version != 1
            || [
                &identity.vendor_name,
                &identity.product_name,
                &identity.serial_number,
            ]
            .iter()
            .any(|value| {
                value.trim().is_empty() || value.len() > 32 || value.chars().any(char::is_control)
            })
        {
            return Err(Error::Invalid);
        }

        let dac = read_bounded(&directory.join("dac.der"), 600)?;
        let pai = read_bounded(&directory.join("pai.der"), 600)?;
        let declaration = read_bounded(&directory.join("certification-declaration.der"), 900)?;
        let mut key = Zeroizing::new([0; 32]);
        let mut input = platform::open_file(&directory.join("dac.key"), false, false)
            .map_err(|_| Error::Unreadable)?;
        input.read_exact(&mut *key).map_err(|_| Error::Invalid)?;
        if input.read(&mut [0; 1]).map_err(|_| Error::Unreadable)? != 0
            || !platform::same_file(&opened, directory).map_err(|_| Error::Unreadable)?
        {
            return Err(Error::Invalid);
        }
        Ok(Self {
            identity,
            dac,
            pai,
            declaration,
            key,
        })
    }

    pub fn identity(&self) -> &ProductIdentity {
        &self.identity
    }

    pub fn dac(&self) -> &[u8] {
        &self.dac
    }

    pub fn pai(&self) -> &[u8] {
        &self.pai
    }

    pub fn declaration(&self) -> &[u8] {
        &self.declaration
    }

    pub fn signing_key(&self) -> &[u8; 32] {
        &self.key
    }

    /// Call after SDK validation. An existing enrollment cannot silently take
    /// on a different certificate or Basic Information identity.
    pub fn bind(&self, store: &mut Store, slot: u16, initializing: bool) -> Result<(), Error> {
        let digest = Sha256::new()
            .chain_update(b"nxrs-matter-attestation-v1")
            .chain_update(self.identity.vendor_id.to_be_bytes())
            .chain_update(self.identity.product_id.to_be_bytes())
            .chain_update(Sha256::digest(&self.dac))
            .chain_update(self.identity.serial_number.as_bytes())
            .finalize();
        match store
            .load(slot, &mut [0u8; 32])
            .map_err(|_| Error::Storage)?
        {
            Some(old) if old == &digest[..] => Ok(()),
            None if initializing => store.store(slot, &digest).map_err(|_| Error::Storage),
            _ => Err(Error::IdentityMismatch),
        }
    }
}

fn read_bounded(path: &Path, limit: usize) -> Result<Vec<u8>, Error> {
    let file = platform::open_file(path, false, false).map_err(|_| Error::Unreadable)?;
    let mut bytes = Vec::new();
    file.take((limit + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| Error::Unreadable)?;
    if bytes.is_empty() || bytes.len() > limit {
        return Err(Error::Invalid);
    }
    Ok(bytes)
}
