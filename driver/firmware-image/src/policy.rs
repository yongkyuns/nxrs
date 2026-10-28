//! Bounded policy checks over an image region. Signature verification is separate.
use crate::{
    manifest, u16_at, u32_at, FormatError, Trust, Version, MAGIC, MAX_IMAGE_BYTES,
    MAX_MANIFEST_BYTES, SIGNATURE_BYTES,
};
use core::num::NonZeroU32;

/// A stable, exclusively controlled image region. Reads fill the entire output
/// or fail; the provider must keep these bytes stable through the caller's later
/// signature validation. The region may include a slot's boot-control trailer.
pub trait ReadAt {
    type Error;
    fn length(&self) -> usize;
    fn read_at(&mut self, offset: usize, output: &mut [u8]) -> Result<(), Self::Error>;
}

impl ReadAt for &[u8] {
    type Error = FormatError;
    fn length(&self) -> usize {
        self.len()
    }
    fn read_at(&mut self, offset: usize, output: &mut [u8]) -> Result<(), FormatError> {
        let end = offset.checked_add(output.len()).ok_or(FormatError::Size)?;
        let bytes = self.get(offset..end).ok_or(FormatError::Size)?;
        output.copy_from_slice(bytes);
        Ok(())
    }
}

/// Trusted incremental SHA-256 implementation. Each digest starts with `reset`.
/// Errors must propagate; no substitution or accepting fallback is permitted.
pub trait IncrementalHash {
    type Error;
    fn reset(&mut self) -> Result<(), Self::Error>;
    fn update(&mut self, bytes: &[u8]) -> Result<(), Self::Error>;
    fn finish(&mut self) -> Result<[u8; 32], Self::Error>;
}

#[derive(Debug, PartialEq, Eq)]
pub enum PolicyError<R, H> {
    Format(FormatError),
    Read(R),
    Hash(H),
}
impl<R, H> From<FormatError> for PolicyError<R, H> {
    fn from(error: FormatError) -> Self {
        Self::Format(error)
    }
}

/// Policy metadata only. This does NOT authenticate a signature or security
/// floor and cannot be converted into `AuthenticatedImage`. A bootloader must
/// still perform its normal root/signature and rollback checks before booting.
#[derive(Debug)]
pub struct PolicyChecked {
    pub(crate) parts: Parts,
    payload_sha256: [u8; 32],
}
impl PolicyChecked {
    pub fn container_bytes(&self) -> usize {
        self.parts.image_end
    }
    pub fn security_counter(&self) -> NonZeroU32 {
        self.parts.counter
    }
    pub fn version(&self) -> Version {
        self.parts.version
    }
    pub fn payload_sha256(&self) -> [u8; 32] {
        self.payload_sha256
    }
}

#[derive(Debug)]
pub(crate) struct Parts {
    pub header: usize,
    pub payload_end: usize,
    pub signed_end: usize,
    pub image_end: usize,
    pub manifest_offset: usize,
    pub manifest_bytes: usize,
    pub signature_offset: usize,
    pub digest: [u8; 32],
    pub counter: NonZeroU32,
    pub version: Version,
}

pub(crate) enum ParseError<E> {
    Format(FormatError),
    Read(E),
}
impl<E> From<FormatError> for ParseError<E> {
    fn from(error: FormatError) -> Self {
        Self::Format(error)
    }
}
fn read<R: ReadAt>(
    source: &mut R,
    offset: usize,
    bytes: &mut [u8],
) -> Result<(), ParseError<R::Error>> {
    if offset
        .checked_add(bytes.len())
        .filter(|end| *end <= source.length())
        .is_none()
    {
        return Err(FormatError::Size.into());
    }
    source.read_at(offset, bytes).map_err(ParseError::Read)
}

struct Entry {
    tag: u16,
    offset: usize,
    length: usize,
}

struct Entries {
    offset: usize,
    end: usize,
}
impl Entries {
    fn new<R: ReadAt>(
        source: &mut R,
        offset: usize,
        magic: u16,
        expected: Option<usize>,
    ) -> Result<Self, ParseError<R::Error>> {
        let mut info = [0; 4];
        if offset
            .checked_add(4)
            .filter(|end| *end <= source.length())
            .is_none()
        {
            return Err(FormatError::Tlv.into());
        }
        read(source, offset, &mut info)?;
        let bytes = usize::from(u16_at(&info, 2));
        let end = offset.checked_add(bytes).ok_or(FormatError::Tlv)?;
        if u16_at(&info, 0) != magic
            || bytes < 4
            || end > source.length()
            || expected.is_some_and(|length| length != bytes)
        {
            return Err(FormatError::Tlv.into());
        }
        Ok(Self {
            offset: offset + 4,
            end,
        })
    }
    fn next<R: ReadAt>(&mut self, source: &mut R) -> Result<Option<Entry>, ParseError<R::Error>> {
        if self.offset == self.end {
            return Ok(None);
        }
        if self
            .offset
            .checked_add(4)
            .filter(|end| *end <= self.end)
            .is_none()
        {
            return Err(FormatError::Tlv.into());
        }
        let mut entry = [0; 4];
        read(source, self.offset, &mut entry)?;
        let bytes = usize::from(u16_at(&entry, 2));
        let offset = self.offset + 4;
        let end = offset.checked_add(bytes).ok_or(FormatError::Tlv)?;
        if bytes == 0 || end > self.end {
            return Err(FormatError::Tlv.into());
        }
        self.offset = end;
        Ok(Some(Entry {
            tag: u16_at(&entry, 0),
            offset,
            length: bytes,
        }))
    }
}

pub(crate) fn parse<R: ReadAt>(
    source: &mut R,
    trust: &Trust<'_>,
) -> Result<Parts, ParseError<R::Error>> {
    let slot = trust.layout.slot_bytes as usize;
    if source.length() < 32 || source.length() > slot || source.length() > MAX_IMAGE_BYTES {
        return Err(FormatError::Size.into());
    }
    let mut raw = [0; 32];
    read(source, 0, &mut raw)?;
    let header = usize::from(u16_at(&raw, 8));
    let protected = usize::from(u16_at(&raw, 10));
    let body = usize::try_from(u32_at(&raw, 12)).map_err(|_| FormatError::Size)?;
    if u32_at(&raw, 0) != MAGIC
        || u32_at(&raw, 4) != 0
        || u32_at(&raw, 16) != 0
        || u32_at(&raw, 28) != 0
        || header != usize::from(trust.layout.header_bytes)
        || body == 0
        || protected > MAX_MANIFEST_BYTES + 16
    {
        return Err(FormatError::Header.into());
    }
    let payload_end = header.checked_add(body).ok_or(FormatError::Size)?;
    let signed_end = payload_end
        .checked_add(protected)
        .ok_or(FormatError::Size)?;
    if signed_end > source.length() {
        return Err(FormatError::Size.into());
    }
    let version = Version {
        major: raw[20],
        minor: raw[21],
        revision: u16_at(&raw, 22),
        build: u32_at(&raw, 24),
    };
    let mut buffer = [0; 128];
    let mut offset = 32;
    while offset < header {
        let length = (header - offset).min(buffer.len());
        read(source, offset, &mut buffer[..length])?;
        if buffer[..length].iter().any(|byte| *byte != 255) {
            return Err(FormatError::Header.into());
        }
        offset += length;
    }
    if body >= 4 {
        read(source, header, &mut buffer[..4])?;
        if buffer[..4] == *b"\x7fELF" || buffer[..4] == MAGIC.to_le_bytes() {
            return Err(FormatError::Header.into());
        }
    }
    let mut entries = Entries::new(source, payload_end, 0x6908, Some(protected))?;
    let mut counter = None;
    let mut manifest = None;
    while let Some(Entry {
        tag,
        offset,
        length,
    }) = entries.next(source)?
    {
        match tag {
            0x50 if counter.is_none() && length == 4 => {
                read(source, offset, &mut buffer[..4])?;
                counter = Some(u32_at(&buffer, 0));
            }
            0xa0 if manifest.is_none() && length <= MAX_MANIFEST_BYTES => {
                manifest = Some((offset, length))
            }
            _ => return Err(FormatError::Tlv.into()),
        }
    }
    let counter = NonZeroU32::new(counter.ok_or(FormatError::Tlv)?).ok_or(FormatError::Tlv)?;
    let (manifest_offset, manifest_bytes) = manifest.ok_or(FormatError::Tlv)?;
    let mut entries = Entries::new(source, signed_end, 0x6907, None)?;
    let image_end = entries.end;
    if image_end > slot || trust.layout.trailer_bytes() > slot - image_end {
        return Err(FormatError::Size.into());
    }
    let mut digest = None;
    let mut key = None;
    let mut signature_offset = None;
    while let Some(Entry {
        tag,
        offset,
        length,
    }) = entries.next(source)?
    {
        match tag {
            0x10 if digest.is_none() && length == 32 => {
                read(source, offset, &mut raw)?;
                digest = Some(raw);
            }
            0x01 if key.is_none() && length == 32 => {
                read(source, offset, &mut raw)?;
                key = Some(raw);
            }
            0x23 if signature_offset.is_none() && length == SIGNATURE_BYTES => {
                signature_offset = Some(offset)
            }
            _ => return Err(FormatError::Tlv.into()),
        }
    }
    let digest = digest.ok_or(FormatError::Tlv)?;
    if key.ok_or(FormatError::Tlv)? != trust.key_sha256 {
        return Err(FormatError::Key.into());
    }
    Ok(Parts {
        header,
        payload_end,
        signed_end,
        image_end,
        manifest_offset,
        manifest_bytes,
        signature_offset: signature_offset.ok_or(FormatError::Tlv)?,
        digest,
        counter,
        version,
    })
}

fn from_parse<R, H>(error: ParseError<R>) -> PolicyError<R, H> {
    match error {
        ParseError::Format(error) => PolicyError::Format(error),
        ParseError::Read(error) => PolicyError::Read(error),
    }
}
fn hash_region<R: ReadAt, H: IncrementalHash>(
    source: &mut R,
    offset: usize,
    length: usize,
    hash: &mut H,
) -> Result<[u8; 32], PolicyError<R::Error, H::Error>> {
    hash.reset().map_err(PolicyError::Hash)?;
    let mut buffer = [0; 128];
    let mut position = offset;
    let end = offset.checked_add(length).ok_or(FormatError::Size)?;
    while position < end {
        let bytes = (end - position).min(buffer.len());
        read(source, position, &mut buffer[..bytes]).map_err(from_parse)?;
        hash.update(&buffer[..bytes]).map_err(PolicyError::Hash)?;
        position += bytes;
    }
    hash.finish().map_err(PolicyError::Hash)
}

/// Check canonical policy and payload/digest relationships with at most 128-byte
/// source reads and no allocation. The source may include a slot trailer: the
/// returned length identifies only the exact image container. Boot-control state,
/// root/signature validation, the security floor and activation are the caller's
/// separate obligations. In particular, this function never authenticates an image.
pub fn check_policy<R: ReadAt, H: IncrementalHash>(
    source: &mut R,
    trust: &Trust<'_>,
    hash: &mut H,
) -> Result<PolicyChecked, PolicyError<R::Error, H::Error>> {
    trust.validate()?;
    let parts = parse(source, trust).map_err(from_parse)?;
    let layout = trust.layout.canonical()?;
    hash.reset().map_err(PolicyError::Hash)?;
    hash.update(layout.bytes()).map_err(PolicyError::Hash)?;
    if hash.finish().map_err(PolicyError::Hash)? != trust.layout_sha256 {
        return Err(FormatError::Policy.into());
    }
    if hash_region(source, 0, parts.signed_end, hash)? != parts.digest {
        return Err(FormatError::Digest.into());
    }
    let payload_sha256 = hash_region(source, parts.header, parts.payload_end - parts.header, hash)?;
    let expected = manifest(trust, &payload_sha256, parts.counter, parts.version)?;
    if parts.manifest_bytes != expected.bytes().len() {
        return Err(FormatError::Manifest.into());
    }
    let mut buffer = [0; 128];
    for (index, bytes) in expected.bytes().chunks(buffer.len()).enumerate() {
        read(
            source,
            parts.manifest_offset + index * buffer.len(),
            &mut buffer[..bytes.len()],
        )
        .map_err(from_parse)?;
        if &buffer[..bytes.len()] != bytes {
            return Err(FormatError::Manifest.into());
        }
    }
    Ok(PolicyChecked {
        parts,
        payload_sha256,
    })
}
