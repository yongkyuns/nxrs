//! Factory-provisioned onboarding data for a gateway Matter node.
use crate::{persistence::Store, platform};
use base64::{engine::general_purpose::STANDARD, Engine};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{io::Read, path::Path};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Error {
    Invalid,
    Unreadable,
    IdentityMismatch,
    Storage,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Record {
    version: u8,
    discriminator: u16,
    iterations: u32,
    salt: String,
    verifier: String,
}

/// Private factory data for the Matter responder. Keep the setup passcode
/// with the commissioner. Deliberately omit Debug and serialization.
#[non_exhaustive]
pub struct FactoryData {
    pub discriminator: u16,
    pub iterations: u32,
    pub salt: Vec<u8>,
    pub verifier: [u8; 97],
}

impl FactoryData {
    pub fn load(path: &Path) -> Result<Self, Error> {
        const MAX: usize = 1024;
        if !path.is_absolute() {
            return Err(Error::Invalid);
        }
        let file = platform::open_file(path, false, false).map_err(|_| Error::Unreadable)?;
        let mut bytes = Vec::new();
        file.take((MAX + 1) as u64)
            .read_to_end(&mut bytes)
            .map_err(|_| Error::Unreadable)?;
        if bytes.len() > MAX {
            return Err(Error::Invalid);
        }
        Self::parse(&bytes)
    }

    fn parse(bytes: &[u8]) -> Result<Self, Error> {
        let r: Record = serde_json::from_slice(bytes).map_err(|_| Error::Invalid)?;
        let salt = STANDARD.decode(r.salt).map_err(|_| Error::Invalid)?;
        let verifier: [u8; 97] = STANDARD
            .decode(r.verifier)
            .map_err(|_| Error::Invalid)?
            .try_into()
            .map_err(|_| Error::Invalid)?;
        if r.version != 1
            || r.discriminator > 4095
            || !(1000..=100000).contains(&r.iterations)
            || !(16..=32).contains(&salt.len())
            || verifier[..32].iter().all(|byte| *byte == 0)
            || verifier[32] != 4
        {
            return Err(Error::Invalid);
        }
        Ok(Self {
            discriminator: r.discriminator,
            iterations: r.iterations,
            salt,
            verifier,
        })
    }

    /// Bind this installation's verifier to its durable state before enabling
    /// network I/O. An incomplete initialization cannot commission after restart.
    pub fn bind(&self, store: &mut Store, key: u16, initializing: bool) -> Result<(), Error> {
        let digest = Sha256::new()
            .chain_update(b"nxrs-matter-onboarding-v1")
            .chain_update(self.discriminator.to_be_bytes())
            .chain_update(self.iterations.to_be_bytes())
            .chain_update(&self.salt)
            .chain_update(self.verifier)
            .finalize();
        match store
            .load(key, &mut [0u8; 32])
            .map_err(|_| Error::Storage)?
        {
            Some(stored) if stored == &digest[..] => Ok(()),
            None if initializing => store.store(key, &digest).map_err(|_| Error::Storage),
            _ => Err(Error::IdentityMismatch),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn accepts_only_versioned_verifier_records() {
        let mut verifier = [1u8; 97];
        verifier[32] = 4;
        let valid = json!({"version":1,"discriminator":4095,"iterations":10000,
            "salt":STANDARD.encode([0u8;16]),"verifier":STANDARD.encode(verifier)});
        assert!(FactoryData::parse(&serde_json::to_vec(&valid).unwrap()).is_ok());
        for (key, value) in [
            ("version", json!(2)),
            ("discriminator", json!(4096)),
            ("iterations", json!(999)),
            ("iterations", json!(100001)),
            ("salt", json!(STANDARD.encode([0u8; 33]))),
            ("verifier", json!("invalid")),
            ("verifier", json!(STANDARD.encode([0u8; 97]))),
            ("passcode", json!(null)),
        ] {
            let mut bad = valid.clone();
            bad[key] = value;
            assert!(matches!(
                FactoryData::parse(&serde_json::to_vec(&bad).unwrap()),
                Err(Error::Invalid)
            ));
        }
    }
}
