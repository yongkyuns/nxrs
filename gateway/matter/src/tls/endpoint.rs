//! Durable TLS Client Management endpoint bindings.

use super::{allocate_id, check_id, ClientCertificates, Error, RootCertificates};
use crate::persistence::{self, Store};
use rustls::pki_types::DnsName;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

pub const MAX_ENDPOINTS: usize = 5;
const CURSOR: u16 = 0x1400;
const RECORDS: std::ops::RangeInclusive<u16> = CURSOR + 1..=CURSOR + 254;
const MAX_RECORD_BYTES: usize = 4096;
const VERSION: u8 = 1;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EndpointSettings {
    pub hostname: String,
    pub port: u16,
    pub root_certificate: u16,
    pub client_certificate: Option<u16>,
}

impl EndpointSettings {
    fn validate(&self) -> Result<(), Error> {
        if !(4..=253).contains(&self.hostname.len())
            || DnsName::try_from(self.hostname.as_str()).is_err()
            || self.port == 0
        {
            return Err(Error::Constraint);
        }
        check_id(self.root_certificate)?;
        if let Some(id) = self.client_certificate {
            check_id(id)?;
        }
        Ok(())
    }

    fn same_address(&self, other: &Self) -> bool {
        self.port == other.port
            && self
                .hostname
                .trim_end_matches('.')
                .eq_ignore_ascii_case(other.hostname.trim_end_matches('.'))
    }
}

#[derive(Debug, PartialEq, Eq)]
pub struct Endpoint {
    pub id: u16,
    pub settings: EndpointSettings,
}

/// An endpoint catalogue scoped to the authenticated fabric supplied by the
/// caller. Configuring a record performs no DNS lookup or network operation.
pub struct Endpoints<'a> {
    store: &'a mut Store,
    fabric: u8,
    key: u16,
}

impl<'a> Endpoints<'a> {
    pub fn open(store: &'a mut Store, fabric: u8) -> Result<Self, Error> {
        let key = Self::record_key(fabric).ok_or(Error::Constraint)?;
        Ok(Self { store, fabric, key })
    }

    pub(super) fn record_key(fabric: u8) -> Option<u16> {
        let key = CURSOR + u16::from(fabric);
        RECORDS.contains(&key).then_some(key)
    }

    pub fn list(&self) -> Result<Vec<Endpoint>, Error> {
        Ok(read(self.store, self.key)?
            .into_iter()
            .map(|(id, settings)| Endpoint { id, settings })
            .collect())
    }

    pub fn find(&self, id: u16) -> Result<Endpoint, Error> {
        check_id(id)?;
        let settings = read(self.store, self.key)?
            .remove(&id)
            .ok_or(Error::NotFound)?;
        Ok(Endpoint { id, settings })
    }

    /// A null ID allocates a new endpoint; an existing ID replaces that record.
    pub fn provision(&mut self, id: Option<u16>, settings: EndpointSettings) -> Result<u16, Error> {
        settings.validate()?;
        if let Some(id) = id {
            check_id(id)?;
        }
        if !RootCertificates::open(self.store, self.fabric)?
            .list()?
            .contains_key(&settings.root_certificate)
        {
            return Err(Error::RootCertificateNotFound);
        }
        if let Some(client) = settings.client_certificate {
            if !ClientCertificates::open(self.store, self.fabric)?.contains(client)? {
                return Err(Error::ClientCertificateNotFound);
            }
        }
        let mut entries = read(self.store, self.key)?;
        if entries
            .iter()
            .any(|(&existing, entry)| Some(existing) != id && settings.same_address(entry))
        {
            return Err(Error::EndpointAlreadyInstalled);
        }

        if let Some(id) = id {
            let record = entries.get_mut(&id).ok_or(Error::NotFound)?;
            *record = settings;
            self.store.store(self.key, &encode(&entries)?)?;
            return Ok(id);
        }
        if entries.len() == MAX_ENDPOINTS {
            return Err(Error::Capacity);
        }
        let mut used = BTreeSet::new();
        for key in self.store.keys().filter(|key| RECORDS.contains(key)) {
            for id in read(self.store, key)?.into_keys() {
                if !used.insert(id) {
                    return Err(persistence::Error::Corrupt.into());
                }
            }
        }
        let id = allocate_id(self.store, CURSOR, &used)?;
        entries.insert(id, settings);
        self.store
            .store_with(self.key, &encode(&entries)?, &[(CURSOR, &id.to_le_bytes())])?;
        Ok(id)
    }

    pub fn remove(&mut self, id: u16) -> Result<(), Error> {
        check_id(id)?;
        let mut entries = read(self.store, self.key)?;
        entries.remove(&id).ok_or(Error::NotFound)?;
        if entries.is_empty() {
            self.store.remove(self.key)?;
        } else {
            self.store.store(self.key, &encode(&entries)?)?;
        }
        Ok(())
    }
}

pub(super) fn references(
    store: &Store,
    fabric: u8,
    predicate: impl FnMut(&EndpointSettings) -> bool,
) -> Result<bool, Error> {
    let key = Endpoints::record_key(fabric).ok_or(Error::Constraint)?;
    Ok(read(store, key)?.values().any(predicate))
}

fn encode(entries: &BTreeMap<u16, EndpointSettings>) -> Result<Vec<u8>, Error> {
    let mut bytes = vec![VERSION];
    serde_json::to_writer(&mut bytes, entries).map_err(|_| persistence::Error::Failed)?;
    Ok(bytes)
}

fn read(store: &Store, key: u16) -> Result<BTreeMap<u16, EndpointSettings>, Error> {
    let Some(bytes) = store.value(key)? else {
        return Ok(BTreeMap::new());
    };
    let corrupt = persistence::Error::Corrupt;
    if bytes.len() > MAX_RECORD_BYTES {
        return Err(corrupt.into());
    }
    let bytes = bytes.strip_prefix(&[VERSION]).ok_or(corrupt)?;
    let entries: BTreeMap<u16, EndpointSettings> =
        serde_json::from_slice(bytes).map_err(|_| corrupt)?;
    if entries.is_empty() || entries.len() > MAX_ENDPOINTS {
        return Err(corrupt.into());
    }
    for (&id, settings) in &entries {
        check_id(id)
            .and_then(|_| settings.validate())
            .map_err(|_| corrupt)?;
    }
    Ok(entries)
}
