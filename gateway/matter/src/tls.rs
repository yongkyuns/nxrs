//! Fabric-scoped credentials for Matter TLS Certificate Management.

use crate::persistence::{self, Store};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use x509_parser::{certificate::X509Certificate, parse_x509_certificate, time::ASN1Time};

mod client;
pub use client::{ClientCertificate, ClientCertificates, SigningRequest, MAX_CLIENT_CERTIFICATES};

pub const MAX_ROOT_CERTIFICATES: usize = 5;
pub const MAX_CERTIFICATE_BYTES: usize = 3000;

const MAX_ID: u16 = u16::MAX - 1;
const ROOT_CURSOR: u16 = 0x1100;
const ROOT_RECORDS: std::ops::RangeInclusive<u16> = ROOT_CURSOR + 1..=ROOT_CURSOR + 254;
const RECORD_VERSION: u8 = 1;

type Entries = BTreeMap<u16, Vec<u8>>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Error {
    Constraint,
    InvalidCertificate,
    InvalidTime,
    AlreadyExists,
    NotFound,
    CertificatePending,
    Crypto,
    Capacity,
    Storage(persistence::Error),
}

impl From<persistence::Error> for Error {
    fn from(error: persistence::Error) -> Self {
        Self::Storage(error)
    }
}

/// Access one authenticated fabric's TLS roots in the node's existing store.
/// The caller must derive `fabric` from the authenticated Matter command.
pub struct RootCertificates<'a> {
    store: &'a mut Store,
    key: u16,
}

impl<'a> RootCertificates<'a> {
    pub fn open(store: &'a mut Store, fabric: u8) -> Result<Self, Error> {
        let key = Self::record_key(fabric).ok_or(Error::Constraint)?;
        Ok(Self { store, key })
    }

    /// The fabric's root record is retired in the same durable write as the fabric.
    pub fn record_key(fabric: u8) -> Option<u16> {
        let key = ROOT_CURSOR + u16::from(fabric);
        ROOT_RECORDS.contains(&key).then_some(key)
    }

    pub fn list(&self) -> Result<BTreeMap<u16, Vec<u8>>, Error> {
        read(self.store, self.key)
    }

    /// Resolve the SHA-256 fingerprint of a DER certificate within this fabric.
    pub fn lookup(&self, fingerprint: &[u8]) -> Result<u16, Error> {
        if fingerprint.len() > 64 {
            return Err(Error::Constraint);
        }
        self.list()?
            .into_iter()
            .find_map(|(id, der)| (Sha256::digest(der).as_slice() == fingerprint).then_some(id))
            .ok_or(Error::NotFound)
    }

    /// Read trust material for one connection using the current synchronized UTC.
    pub fn trust_anchor(&self, id: u16, unix_seconds: Option<i64>) -> Result<Vec<u8>, Error> {
        check_id(id)?;
        let mut entries = self.list()?;
        let der = entries.remove(&id).ok_or(Error::NotFound)?;
        validate(&der, unix_seconds)?;
        Ok(der)
    }

    /// Install a root or rotate the root under an existing CAID. The clock is
    /// supplied by the node so an unsynchronized clock cannot admit a certificate.
    pub fn provision(
        &mut self,
        id: Option<u16>,
        der: &[u8],
        unix_seconds: Option<i64>,
    ) -> Result<u16, Error> {
        if !(1..=MAX_CERTIFICATE_BYTES).contains(&der.len()) {
            return Err(Error::Constraint);
        }
        let mut entries = self.list()?;
        if entries.values().any(|entry| entry == der) {
            return Err(Error::AlreadyExists);
        }
        if let Some(id) = id {
            check_id(id)?;
            if !entries.contains_key(&id) {
                return Err(Error::NotFound);
            }
        } else if entries.len() == MAX_ROOT_CERTIFICATES {
            return Err(Error::Capacity);
        }
        validate(der, unix_seconds)?;

        let (id, allocate) = match id {
            Some(id) => (id, false),
            None => (next_available_id(self.store)?, true),
        };
        entries.insert(id, der.to_vec());
        let bytes = encode(&entries);
        if allocate {
            self.store
                .store_with(self.key, &bytes, &[(ROOT_CURSOR, &id.to_le_bytes())])?;
        } else {
            self.store.store(self.key, &bytes)?;
        }
        Ok(id)
    }

    /// Remove an unreferenced root. The cluster must check endpoint references
    /// under the same state owner before calling this operation.
    pub fn remove(&mut self, id: u16) -> Result<(), Error> {
        check_id(id)?;
        let mut entries = self.list()?;
        entries.remove(&id).ok_or(Error::NotFound)?;
        if entries.is_empty() {
            self.store.remove(self.key)?;
        } else {
            self.store.store(self.key, &encode(&entries))?;
        }
        Ok(())
    }
}

fn check_id(id: u16) -> Result<(), Error> {
    (id <= MAX_ID).then_some(()).ok_or(Error::Constraint)
}

fn validate(der: &[u8], unix_seconds: Option<i64>) -> Result<(), Error> {
    let cert = parse_certificate(der)?;
    let constraints = cert
        .basic_constraints()
        .map_err(|_| Error::InvalidCertificate)?;
    let usage = cert.key_usage().map_err(|_| Error::InvalidCertificate)?;
    if cert.subject() != cert.issuer()
        || !constraints.is_some_and(|constraints| constraints.value.ca)
        || usage.is_some_and(|usage| !usage.value.key_cert_sign())
        || cert.verify_signature(None).is_err()
    {
        return Err(Error::InvalidCertificate);
    }
    check_validity(&cert, unix_seconds)
}

fn parse_certificate(der: &[u8]) -> Result<X509Certificate<'_>, Error> {
    let (rest, certificate) = parse_x509_certificate(der).map_err(|_| Error::InvalidCertificate)?;
    if !rest.is_empty() {
        return Err(Error::InvalidCertificate);
    }
    Ok(certificate)
}

fn check_validity(cert: &X509Certificate<'_>, unix_seconds: Option<i64>) -> Result<(), Error> {
    let now = unix_seconds
        .and_then(|seconds| ASN1Time::from_timestamp(seconds).ok())
        .ok_or(Error::InvalidTime)?;
    if !cert.validity().is_valid_at(now) {
        return Err(Error::InvalidCertificate);
    }
    Ok(())
}

fn next_available_id(store: &Store) -> Result<u16, Error> {
    let mut used = BTreeSet::new();
    for key in store.keys().filter(|key| ROOT_RECORDS.contains(key)) {
        for id in read(store, key)?.into_keys() {
            if !used.insert(id) {
                return Err(persistence::Error::Corrupt.into());
            }
        }
    }
    allocate_id(store, ROOT_CURSOR, &used)
}

fn allocate_id(store: &Store, cursor_key: u16, used: &BTreeSet<u16>) -> Result<u16, Error> {
    let mut candidate = match store.value(cursor_key)? {
        None => 0,
        Some(bytes) => {
            let cursor =
                u16::from_le_bytes(bytes.try_into().map_err(|_| persistence::Error::Corrupt)?);
            check_id(cursor).map_err(|_| persistence::Error::Corrupt)?;
            advance(cursor)
        }
    };
    // The store's total record bound is much smaller than the CAID space.
    for _ in 0..=used.len() {
        if !used.contains(&candidate) {
            return Ok(candidate);
        }
        candidate = advance(candidate);
    }
    Err(Error::Capacity)
}

fn advance(id: u16) -> u16 {
    if id == MAX_ID {
        0
    } else {
        id + 1
    }
}

fn encode(entries: &Entries) -> Vec<u8> {
    let mut bytes = vec![RECORD_VERSION];
    for (&id, der) in entries {
        bytes.extend_from_slice(&id.to_le_bytes());
        bytes.extend_from_slice(&(der.len() as u16).to_le_bytes());
        bytes.extend_from_slice(der);
    }
    bytes
}

fn read(store: &Store, key: u16) -> Result<Entries, Error> {
    let mut entries = BTreeMap::new();
    let Some(bytes) = store.value(key)? else {
        return Ok(entries);
    };
    let corrupt = persistence::Error::Corrupt;
    let mut bytes = bytes.strip_prefix(&[RECORD_VERSION]).ok_or(corrupt)?;
    while !bytes.is_empty() {
        let (header, remaining) = bytes.split_at_checked(4).ok_or(corrupt)?;
        let id = u16::from_le_bytes([header[0], header[1]]);
        let len = usize::from(u16::from_le_bytes([header[2], header[3]]));
        if entries.len() == MAX_ROOT_CERTIFICATES
            || entries
                .last_key_value()
                .is_some_and(|(&last, _)| id <= last)
            || id > MAX_ID
            || !(1..=MAX_CERTIFICATE_BYTES).contains(&len)
        {
            return Err(corrupt.into());
        }
        let (der, remaining) = remaining.split_at_checked(len).ok_or(corrupt)?;
        entries.insert(id, der.to_vec());
        bytes = remaining;
    }
    if entries.is_empty() {
        return Err(corrupt.into());
    }
    Ok(entries)
}
