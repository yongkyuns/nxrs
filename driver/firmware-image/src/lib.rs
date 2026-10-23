//! Bounded MCUboot/nxrs image authentication. No flash or activation operations.
//!
//! The caller supplies an independently trusted policy, a trusted cryptographic
//! implementation and the current security floor. A successful result authenticates
//! these bytes only; protected-state provenance and activation remain the owner's job.
#![no_std]
#![forbid(unsafe_code)]

use core::fmt::{self, Write};
use core::num::NonZeroU32;

pub const SIGNATURE_BYTES: usize = 384;
pub const MAX_MANIFEST_BYTES: usize = 1024;
pub const MAX_IMAGE_BYTES: usize = 16 * 1024 * 1024;
const MAGIC: u32 = 0x96f3_b83d;

/// This is a trusted dependency, selected with the policy by the application.
/// It must verify RSA-3072/PSS/SHA-256 with MGF1/SHA-256 and exactly 32 salt bytes.
/// The root must remain fixed for this instance. `key_sha256` identifies that same
/// root; it must be derived from the actual verification key, never copied from
/// the incoming image or the expected policy. The implementation remains trusted.
pub trait Crypto {
    type Error;
    /// SHA-256 of the canonical PKCS#1 DER public key actually used for verification.
    /// Constructors must refuse any failure to obtain this stable identity.
    fn key_sha256(&self) -> [u8; 32];
    fn sha256(&mut self, message: &[u8]) -> Result<[u8; 32], Self::Error>;
    fn verify_rsa3072_pss_sha256(
        &mut self,
        digest: &[u8; 32],
        signature: &[u8; SIGNATURE_BYTES],
    ) -> Result<(), Self::Error>;
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Layout {
    pub flash_bytes: u32,
    pub primary_offset: u32,
    pub secondary_offset: u32,
    pub slot_bytes: u32,
    pub scratch_offset: u32,
    pub scratch_bytes: u32,
    pub erase_bytes: u32,
    pub header_bytes: u16,
    pub write_align: u8,
    pub max_align: u8,
    pub max_sectors: u16,
}

impl Layout {
    pub fn validate(&self) -> Result<(), FormatError> {
        let erase = self.erase_bytes;
        if !(65536..=MAX_IMAGE_BYTES as u32).contains(&self.flash_bytes)
            || !(4096..=65536).contains(&erase)
            || !erase.is_power_of_two()
            || self.slot_bytes < erase * 4
            || self.slot_bytes > self.flash_bytes
            || self.scratch_bytes < erase
            || !matches!(self.header_bytes, 32 | 512 | 1024 | 4096)
            || u32::from(self.header_bytes) >= self.slot_bytes
            || !matches!(self.write_align, 4 | 8)
            || self.max_align != 8
            || u32::from(self.max_sectors) < self.slot_bytes / erase
            || self.max_sectors > 4096
        {
            return Err(FormatError::Policy);
        }
        let ranges = [
            (self.primary_offset, self.slot_bytes),
            (self.secondary_offset, self.slot_bytes),
            (self.scratch_offset, self.scratch_bytes),
        ];
        for (index, &(offset, length)) in ranges.iter().enumerate() {
            if offset < erase
                || offset % erase != 0
                || length % erase != 0
                || u64::from(offset) + u64::from(length) > u64::from(self.flash_bytes)
            {
                return Err(FormatError::Policy);
            }
            for &(other, size) in &ranges[..index] {
                if u64::from(offset) < u64::from(other) + u64::from(size)
                    && u64::from(other) < u64::from(offset) + u64::from(length)
                {
                    return Err(FormatError::Policy);
                }
            }
        }
        Ok(())
    }

    fn trailer_bytes(&self) -> usize {
        // Validated fields bound this to fewer than 100 KiB on a 32-bit target.
        3 * usize::from(self.max_sectors) * usize::from(self.write_align) + 48
    }

    fn canonical(&self) -> Result<Buffer<512>, FormatError> {
        let mut buffer = Buffer::new();
        write!(&mut buffer,
            "{{\"erase_bytes\":{},\"flash_bytes\":{},\"header_bytes\":{},\"max_align\":{},\"max_sectors\":{},\"primary_offset\":{},\"scratch_bytes\":{},\"scratch_offset\":{},\"secondary_offset\":{},\"slot_bytes\":{},\"write_align\":{}}}",
            self.erase_bytes, self.flash_bytes, self.header_bytes, self.max_align, self.max_sectors,
            self.primary_offset, self.scratch_bytes, self.scratch_offset, self.secondary_offset,
            self.slot_bytes, self.write_align).map_err(|_| FormatError::Policy)?;
        Ok(buffer)
    }
}

/// Configuration obtained independently of the incoming image or download peer.
/// Hashes use the exact signed public policy and canonical layout from the signer.
#[derive(Clone, Copy, Debug)]
pub struct Trust<'a> {
    pub target: &'a str,
    pub policy_sha256: [u8; 32],
    pub layout_sha256: [u8; 32],
    pub configuration_sha256: [u8; 32],
    /// SHA-256 of the approved PKCS#1 DER RSA public key, matching the crypto root.
    pub key_sha256: [u8; 32],
    pub layout: Layout,
}

impl Trust<'_> {
    /// Validate structural policy fields; authentication also checks the layout digest.
    pub fn validate(&self) -> Result<(), FormatError> {
        self.layout.validate()?;
        let bytes = self.target.as_bytes();
        if bytes.is_empty()
            || bytes.len() > 64
            || !bytes[0].is_ascii_lowercase() && !bytes[0].is_ascii_digit()
            || !bytes.iter().all(|byte| {
                byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'-')
            })
        {
            return Err(FormatError::Policy);
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Version {
    pub major: u8,
    pub minor: u8,
    pub revision: u16,
    pub build: u32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FormatError {
    Policy,
    Size,
    Header,
    Tlv,
    Key,
    Digest,
    Manifest,
    SecurityFloor,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Error<E> {
    Format(FormatError),
    Crypto(E),
}

impl<E> From<FormatError> for Error<E> {
    fn from(error: FormatError) -> Self {
        Self::Format(error)
    }
}

/// An immutable borrow of the exact authenticated image, with no public constructor.
/// This is not authorization to erase, activate, confirm or advance protected state.
#[derive(Debug)]
pub struct AuthenticatedImage<'a> {
    image: &'a [u8],
    header: usize,
    payload_end: usize,
    payload_sha256: [u8; 32],
    security_counter: NonZeroU32,
    version: Version,
}

impl<'a> AuthenticatedImage<'a> {
    pub fn bytes(&self) -> &'a [u8] {
        self.image
    }
    pub fn payload(&self) -> &'a [u8] {
        &self.image[self.header..self.payload_end]
    }
    pub fn payload_sha256(&self) -> [u8; 32] {
        self.payload_sha256
    }
    pub fn security_counter(&self) -> NonZeroU32 {
        self.security_counter
    }
    pub fn version(&self) -> Version {
        self.version
    }
}

fn u16_at(data: &[u8], offset: usize) -> u16 {
    u16::from_le_bytes([data[offset], data[offset + 1]])
}
fn u32_at(data: &[u8], offset: usize) -> u32 {
    u32::from_le_bytes([
        data[offset],
        data[offset + 1],
        data[offset + 2],
        data[offset + 3],
    ])
}

struct Entries<'a> {
    bytes: &'a [u8],
    offset: usize,
}
impl<'a> Entries<'a> {
    fn new(bytes: &'a [u8], magic: u16) -> Result<Self, FormatError> {
        if bytes.len() < 4
            || u16_at(bytes, 0) != magic
            || usize::from(u16_at(bytes, 2)) != bytes.len()
        {
            return Err(FormatError::Tlv);
        }
        Ok(Self { bytes, offset: 4 })
    }
    fn next(&mut self) -> Result<Option<(u16, &'a [u8])>, FormatError> {
        if self.offset == self.bytes.len() {
            return Ok(None);
        }
        let header = self
            .bytes
            .get(self.offset..self.offset + 4)
            .ok_or(FormatError::Tlv)?;
        let tag = u16_at(header, 0);
        let length = usize::from(u16_at(header, 2));
        if length == 0 {
            return Err(FormatError::Tlv);
        }
        self.offset += 4;
        let value = self
            .bytes
            .get(self.offset..self.offset + length)
            .ok_or(FormatError::Tlv)?;
        self.offset += length;
        Ok(Some((tag, value)))
    }
}

fn manifest(
    trust: &Trust<'_>,
    payload_sha: &[u8; 32],
    counter: NonZeroU32,
    version: Version,
) -> Result<Buffer<MAX_MANIFEST_BYTES>, FormatError> {
    let mut buffer = Buffer::new();
    write!(&mut buffer,
        "{{\"configuration_sha256\":\"{}\",\"layout_sha256\":\"{}\",\"payload_sha256\":\"{}\",\"policy_sha256\":\"{}\",\"role\":\"nxrs-application-image\",\"schema\":1,\"security_counter\":{},\"target\":\"{}\",\"version\":[{},{},{},{}]}}",
        Hex(&trust.configuration_sha256), Hex(&trust.layout_sha256), Hex(payload_sha),
        Hex(&trust.policy_sha256), counter.get(), trust.target,
        version.major, version.minor, version.revision, version.build).map_err(|_| FormatError::Manifest)?;
    Ok(buffer)
}

/// Authenticate the exact unpadded, unconfirmed container emitted by the offline signer.
/// `minimum_counter` must come from the caller's trusted security-state boundary.
/// Successful authentication does not prove that the supplied floor is protected.
pub fn authenticate<'a, C: Crypto>(
    image: &'a [u8],
    trust: &Trust<'_>,
    minimum_counter: NonZeroU32,
    crypto: &mut C,
) -> Result<AuthenticatedImage<'a>, Error<C::Error>> {
    trust.validate()?;
    // A provider/policy mismatch is a caller configuration fault, independently
    // of untrusted image metadata. Refuse it before hashing or verifying an image.
    if crypto.key_sha256() != trust.key_sha256 {
        return Err(FormatError::Policy.into());
    }
    let slot = trust.layout.slot_bytes as usize;
    if image.len() < 32
        || image.len() > slot
        || image.len() > MAX_IMAGE_BYTES
        || trust.layout.trailer_bytes() > slot - image.len()
    {
        return Err(FormatError::Size.into());
    }
    let header = usize::from(u16_at(image, 8));
    let protected_bytes = usize::from(u16_at(image, 10));
    let body = usize::try_from(u32_at(image, 12)).map_err(|_| FormatError::Size)?;
    if u32_at(image, 0) != MAGIC
        || u32_at(image, 4) != 0
        || u32_at(image, 16) != 0
        || u32_at(image, 28) != 0
        || header != usize::from(trust.layout.header_bytes)
        || body == 0
        || protected_bytes > MAX_MANIFEST_BYTES + 16
    {
        return Err(FormatError::Header.into());
    }
    let payload_end = header.checked_add(body).ok_or(FormatError::Size)?;
    let signed_end = payload_end
        .checked_add(protected_bytes)
        .ok_or(FormatError::Size)?;
    let payload = image.get(header..payload_end).ok_or(FormatError::Size)?;
    let protected = image
        .get(payload_end..signed_end)
        .ok_or(FormatError::Size)?;
    if image
        .get(32..header)
        .ok_or(FormatError::Header)?
        .iter()
        .any(|byte| *byte != 255)
        || payload.starts_with(b"\x7fELF")
        || payload.starts_with(&MAGIC.to_le_bytes())
    {
        return Err(FormatError::Header.into());
    }
    let version = Version {
        major: image[20],
        minor: image[21],
        revision: u16_at(image, 22),
        build: u32_at(image, 24),
    };
    let mut protected_entries = Entries::new(protected, 0x6908)?;
    let mut counter = None;
    let mut supplied_manifest = None;
    while let Some((tag, value)) = protected_entries.next()? {
        match tag {
            0x50 if counter.is_none() && value.len() == 4 => counter = Some(u32_at(value, 0)),
            0xa0 if supplied_manifest.is_none() && value.len() <= MAX_MANIFEST_BYTES => {
                supplied_manifest = Some(value)
            }
            _ => return Err(FormatError::Tlv.into()),
        }
    }
    let counter = NonZeroU32::new(counter.ok_or(FormatError::Tlv)?).ok_or(FormatError::Tlv)?;
    let supplied_manifest = supplied_manifest.ok_or(FormatError::Tlv)?;
    let mut entries = Entries::new(image.get(signed_end..).ok_or(FormatError::Size)?, 0x6907)?;
    let mut digest = None;
    let mut key = None;
    let mut signature = None;
    while let Some((tag, value)) = entries.next()? {
        match tag {
            0x10 if digest.is_none() && value.len() == 32 => digest = Some(value),
            0x01 if key.is_none() && value.len() == 32 => key = Some(value),
            0x23 if signature.is_none() && value.len() == SIGNATURE_BYTES => {
                signature = Some(value)
            }
            _ => return Err(FormatError::Tlv.into()),
        }
    }
    let digest = digest.ok_or(FormatError::Tlv)?;
    if key.ok_or(FormatError::Tlv)? != trust.key_sha256 {
        return Err(FormatError::Key.into());
    }
    let signature: &[u8; SIGNATURE_BYTES] = signature
        .ok_or(FormatError::Tlv)?
        .try_into()
        .map_err(|_| FormatError::Tlv)?;
    let canonical_layout = trust.layout.canonical()?;
    if crypto
        .sha256(canonical_layout.bytes())
        .map_err(Error::Crypto)?
        != trust.layout_sha256
    {
        return Err(FormatError::Policy.into());
    }
    let actual = crypto.sha256(&image[..signed_end]).map_err(Error::Crypto)?;
    if actual != digest {
        return Err(FormatError::Digest.into());
    }
    crypto
        .verify_rsa3072_pss_sha256(&actual, signature)
        .map_err(Error::Crypto)?;
    let payload_sha256 = crypto.sha256(payload).map_err(Error::Crypto)?;
    let expected = manifest(trust, &payload_sha256, counter, version)?;
    if supplied_manifest != expected.bytes() {
        return Err(FormatError::Manifest.into());
    }
    if counter < minimum_counter {
        return Err(FormatError::SecurityFloor.into());
    }
    Ok(AuthenticatedImage {
        image,
        header,
        payload_end,
        payload_sha256,
        security_counter: counter,
        version,
    })
}

struct Buffer<const N: usize> {
    value: [u8; N],
    length: usize,
}
impl<const N: usize> Buffer<N> {
    const fn new() -> Self {
        Self {
            value: [0; N],
            length: 0,
        }
    }
    fn bytes(&self) -> &[u8] {
        &self.value[..self.length]
    }
}
impl<const N: usize> Write for Buffer<N> {
    fn write_str(&mut self, value: &str) -> fmt::Result {
        let end = self.length.checked_add(value.len()).ok_or(fmt::Error)?;
        let destination = self.value.get_mut(self.length..end).ok_or(fmt::Error)?;
        destination.copy_from_slice(value.as_bytes());
        self.length = end;
        Ok(())
    }
}
struct Hex<'a>(&'a [u8; 32]);
impl fmt::Display for Hex<'_> {
    fn fmt(&self, output: &mut fmt::Formatter<'_>) -> fmt::Result {
        for byte in self.0 {
            write!(output, "{byte:02x}")?;
        }
        Ok(())
    }
}
