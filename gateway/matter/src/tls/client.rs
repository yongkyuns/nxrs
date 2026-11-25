use super::{
    allocate_id, check_id, check_validity, parse_certificate, Error, MAX_CERTIFICATE_BYTES,
};
use crate::persistence::{self, Store};
use rcgen::CertificateParams;
use ring::{
    rand::SystemRandom,
    signature::{EcdsaKeyPair, ECDSA_P256_SHA256_FIXED_SIGNING},
};
use rustls::{
    crypto::ring::sign::any_ecdsa_type,
    pki_types::{CertificateDer, PrivatePkcs8KeyDer},
    sign::CertifiedKey,
};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use zeroize::Zeroizing;

pub const MAX_CLIENT_CERTIFICATES: usize = 2;
pub const MAX_INTERMEDIATE_CERTIFICATES: usize = 10;
const MAX_KEY_BYTES: usize = 256;
const CURSOR: u16 = 0x1200;
const RECORDS: std::ops::RangeInclusive<u16> = CURSOR + 1..=CURSOR + 254 * 2;
const VERSION: u8 = 1;

pub struct SigningRequest {
    pub id: u16,
    pub der: Vec<u8>,
    pub nonce_signature: [u8; 64],
}

#[derive(Debug, PartialEq, Eq)]
pub struct ClientCertificate {
    pub id: u16,
    /// Pending CSRs appear with a null certificate.
    pub leaf: Option<Vec<u8>>,
    pub intermediates: Vec<Vec<u8>>,
}

/// A table within one authenticated fabric. The caller supplies the fabric
/// from the authorized Matter interaction. Private DER stays in the store.
pub struct ClientCertificates<'a> {
    store: &'a mut Store,
    fabric: u8,
    records: [u16; MAX_CLIENT_CERTIFICATES],
}

impl<'a> ClientCertificates<'a> {
    pub fn open(store: &'a mut Store, fabric: u8) -> Result<Self, Error> {
        let records = Self::record_keys(fabric).ok_or(Error::Constraint)?;
        Ok(Self {
            store,
            fabric,
            records,
        })
    }

    /// Include these records in the transaction that retires their fabric.
    pub fn record_keys(fabric: u8) -> Option<[u16; MAX_CLIENT_CERTIFICATES]> {
        let first = CURSOR + 2 * u16::from(fabric.checked_sub(1)?) + 1;
        RECORDS.contains(&(first + 1)).then_some([first, first + 1])
    }

    /// A null ID allocates a key. Renewal reuses the existing key and keeps the
    /// current certificate usable until its replacement is stored.
    pub fn request_csr(&mut self, id: Option<u16>, nonce: &[u8]) -> Result<SigningRequest, Error> {
        if nonce.len() != 32 {
            return Err(Error::Constraint);
        }
        if let Some(id) = id {
            let (_, record) = self.find(id)?;
            let key = rcgen::KeyPair::try_from(&PrivatePkcs8KeyDer::from(record.key))
                .map_err(|_| persistence::Error::Corrupt)?;
            return sign_request(id, &Zeroizing::new(key), nonce);
        }

        let slot = self.available_slot()?;
        let mut used = BTreeSet::new();
        for slot in self.store.keys().filter(|slot| RECORDS.contains(slot)) {
            if let Some(entry) = read(self.store, slot)? {
                if !used.insert(entry.id) {
                    return Err(persistence::Error::Corrupt.into());
                }
            }
        }
        let id = allocate_id(self.store, CURSOR, &used)?;
        let key = Zeroizing::new(rcgen::KeyPair::generate().map_err(|_| Error::Crypto)?);
        let request = sign_request(id, &key, nonce)?;
        let bytes = encode(id, key.serialized_der(), &[]);
        self.store
            .store_with(slot, &bytes, &[(CURSOR, &id.to_le_bytes())])?;
        Ok(request)
    }

    pub fn list(&self) -> Result<Vec<ClientCertificate>, Error> {
        self.entries()
            .map(|result| result.map(|(_, entry)| entry.public_details()))
            .collect()
    }

    pub fn lookup(&self, fingerprint: &[u8]) -> Result<u16, Error> {
        if fingerprint.len() > 64 {
            return Err(Error::Constraint);
        }
        for result in self.entries() {
            let (_, entry) = result?;
            if entry
                .certificates
                .first()
                .is_some_and(|der| Sha256::digest(der).as_slice() == fingerprint)
            {
                return Ok(entry.id);
            }
        }
        Err(Error::NotFound)
    }

    pub fn provision(
        &mut self,
        id: u16,
        leaf: &[u8],
        intermediates: &[&[u8]],
        unix_seconds: Option<i64>,
    ) -> Result<(), Error> {
        if intermediates.len() > MAX_INTERMEDIATE_CERTIFICATES {
            return Err(Error::Constraint);
        }
        let chain: Vec<_> = std::iter::once(leaf)
            .chain(intermediates.iter().copied())
            .collect();
        for der in &chain {
            if !(1..=MAX_CERTIFICATE_BYTES).contains(&der.len()) {
                return Err(Error::Constraint);
            }
            check_validity(&parse_certificate(der)?, unix_seconds)?;
        }
        let (slot, entry) = self.find(id)?;
        for result in self.entries() {
            if result?.1.certificates.first() == Some(&leaf) {
                return Err(Error::AlreadyExists);
            }
        }
        // Proof of possession is tied to the generated key. The peer validates
        // the certificate chain against its client trust policy on connection.
        certified_key(entry.key, &chain)?;
        let bytes = encode(id, entry.key, &chain);
        self.store.store(slot, &bytes)?;
        Ok(())
    }

    /// Provide the TLS engine with a signing handle and certificates for one
    /// connection. A retired endpoint must drop the handle with its connection.
    pub fn identity(&self, id: u16, unix_seconds: Option<i64>) -> Result<CertifiedKey, Error> {
        let (_, entry) = self.find(id)?;
        if entry.certificates.is_empty() {
            return Err(Error::CertificatePending);
        }
        for der in &entry.certificates {
            check_validity(&parse_certificate(der)?, unix_seconds)?;
        }
        certified_key(entry.key, &entry.certificates)
    }

    /// Remove a certificate and key once its endpoints are removed or changed.
    pub fn remove(&mut self, id: u16) -> Result<(), Error> {
        let (slot, _) = self.find(id)?;
        if super::endpoint::references(self.store, self.fabric, |settings| {
            settings.client_certificate == Some(id)
        })? {
            return Err(Error::InUse);
        }
        Ok(self.store.remove(slot)?)
    }

    pub(super) fn contains(&self, id: u16) -> Result<bool, Error> {
        match self.find(id) {
            Ok(_) => Ok(true),
            Err(Error::NotFound) => Ok(false),
            Err(error) => Err(error),
        }
    }

    fn entries(&self) -> impl Iterator<Item = Result<(u16, Entry<'_>), Error>> {
        self.records.into_iter().filter_map(|slot| {
            read(self.store, slot)
                .transpose()
                .map(|result| result.map(|entry| (slot, entry)))
        })
    }

    fn find(&self, id: u16) -> Result<(u16, Entry<'_>), Error> {
        check_id(id)?;
        for result in self.entries() {
            let (slot, entry) = result?;
            if entry.id == id {
                return Ok((slot, entry));
            }
        }
        Err(Error::NotFound)
    }

    fn available_slot(&self) -> Result<u16, Error> {
        for slot in self.records {
            if self.store.value(slot)?.is_none() {
                return Ok(slot);
            }
        }
        Err(Error::Capacity)
    }
}

fn sign_request(id: u16, key: &rcgen::KeyPair, nonce: &[u8]) -> Result<SigningRequest, Error> {
    let request = CertificateParams::default()
        .serialize_request(key)
        .map_err(|_| Error::Crypto)?;
    let rng = SystemRandom::new();
    let sign =
        EcdsaKeyPair::from_pkcs8(&ECDSA_P256_SHA256_FIXED_SIGNING, key.serialized_der(), &rng)
            .map_err(|_| Error::Crypto)?;
    let nonce_signature = sign.sign(&rng, nonce).map_err(|_| Error::Crypto)?;
    Ok(SigningRequest {
        id,
        der: request.der().as_ref().to_vec(),
        nonce_signature: nonce_signature
            .as_ref()
            .try_into()
            .map_err(|_| Error::Crypto)?,
    })
}

fn certified_key(key: &[u8], certificates: &[&[u8]]) -> Result<CertifiedKey, Error> {
    let key = any_ecdsa_type(&PrivatePkcs8KeyDer::from(key).into())
        .map_err(|_| persistence::Error::Corrupt)?;
    let chain = certificates
        .iter()
        .map(|der| CertificateDer::from(der.to_vec()))
        .collect();
    let identity = CertifiedKey::new(chain, key);
    identity
        .keys_match()
        .map_err(|_| Error::InvalidCertificate)?;
    Ok(identity)
}

struct Entry<'a> {
    id: u16,
    key: &'a [u8],
    certificates: Vec<&'a [u8]>,
}

impl Entry<'_> {
    fn public_details(&self) -> ClientCertificate {
        let mut chain = self.certificates.iter().map(|der| der.to_vec());
        ClientCertificate {
            id: self.id,
            leaf: chain.next(),
            intermediates: chain.collect(),
        }
    }
}

fn encode(id: u16, key: &[u8], certificates: &[&[u8]]) -> Zeroizing<Vec<u8>> {
    let size = 6 + key.len() + certificates.iter().map(|der| der.len() + 2).sum::<usize>();
    let mut bytes = Zeroizing::new(Vec::with_capacity(size));
    bytes.push(VERSION);
    bytes.extend_from_slice(&id.to_le_bytes());
    bytes.extend_from_slice(&(key.len() as u16).to_le_bytes());
    bytes.extend_from_slice(key);
    bytes.push(certificates.len() as u8);
    for der in certificates {
        bytes.extend_from_slice(&(der.len() as u16).to_le_bytes());
        bytes.extend_from_slice(der);
    }
    bytes
}

fn read(store: &Store, slot: u16) -> Result<Option<Entry<'_>>, Error> {
    let Some(bytes) = store.value(slot)? else {
        return Ok(None);
    };
    let corrupt = persistence::Error::Corrupt;
    let bytes = bytes.strip_prefix(&[VERSION]).ok_or(corrupt)?;
    let (id, bytes) = bytes.split_at_checked(2).ok_or(corrupt)?;
    let id = u16::from_le_bytes([id[0], id[1]]);
    check_id(id).map_err(|_| corrupt)?;
    let (key, bytes) = take_field(bytes, MAX_KEY_BYTES)?;
    let (count, mut bytes) = bytes.split_first().ok_or(corrupt)?;
    let count = usize::from(*count);
    if count > MAX_INTERMEDIATE_CERTIFICATES + 1 {
        return Err(corrupt.into());
    }
    let mut certificates = Vec::with_capacity(count);
    for _ in 0..count {
        let (certificate, rest) = take_field(bytes, MAX_CERTIFICATE_BYTES)?;
        certificates.push(certificate);
        bytes = rest;
    }
    if !bytes.is_empty() {
        return Err(corrupt.into());
    }
    Ok(Some(Entry {
        id,
        key,
        certificates,
    }))
}

fn take_field(bytes: &[u8], max: usize) -> Result<(&[u8], &[u8]), persistence::Error> {
    let corrupt = persistence::Error::Corrupt;
    let (length, bytes) = bytes.split_at_checked(2).ok_or(corrupt)?;
    let length = usize::from(u16::from_le_bytes([length[0], length[1]]));
    if !(1..=max).contains(&length) {
        return Err(corrupt);
    }
    bytes.split_at_checked(length).ok_or(corrupt)
}
