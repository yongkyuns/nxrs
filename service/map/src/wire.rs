//! Bounded tile request and reply protocol for the phone and IP relay.
use std::io::{self, Read};

/// Width and height of a decoded tile, in pixels.
pub const SIDE: usize = 170;
const PALETTE_BYTES: usize = 32;
const ROW_BYTES: usize = SIDE / 2;
const PIXEL_BYTES: usize = ROW_BYTES * SIDE;
const METADATA_BYTES: usize = PALETTE_BYTES + SIDE;
const BLOCK_BYTES: usize = 1024;
const BLOCKS: usize = PIXEL_BYTES.div_ceil(BLOCK_BYTES);
const MAGIC: &[u8; 4] = b"NXM5";
/// Length of an `NXM5` tile request and of a response's key prefix.
pub const REQUEST_BYTES: usize = 13;

/// Supported Web Mercator zoom level, from 15 through 18 inclusive.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Zoom(u8);

impl Zoom {
    /// Validate a zoom level supported by the tile protocol.
    pub fn new(level: u8) -> Option<Self> {
        (15..=18).contains(&level).then_some(Self(level))
    }

    /// Return the numeric zoom level.
    pub fn level(self) -> u8 {
        self.0
    }

    /// Number of tiles along either axis at this zoom.
    pub fn tiles(self) -> u32 {
        1 << self.level()
    }
}

impl Default for Zoom {
    fn default() -> Self {
        Self(18)
    }
}

/// Slippy-map tile coordinates, with x increasing east and y increasing south.
///
/// Each coordinate must be less than [`Zoom::tiles`]. Use [`Key::new`] when
/// accepting coordinates from a caller; the wire receiver validates them too.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Key {
    pub zoom: Zoom,
    pub x: u32,
    pub y: u32,
}

impl Key {
    /// Construct a key only when both coordinates lie in this zoom's world.
    pub fn new(zoom: Zoom, x: u32, y: u32) -> Option<Self> {
        let key = Self { zoom, x, y };
        key.is_valid().then_some(key)
    }

    /// Check the coordinate bounds of a key constructed through its fields.
    pub fn is_valid(self) -> bool {
        self.x < self.zoom.tiles() && self.y < self.zoom.tiles()
    }

    /// Encode this key as an `NXM5` request without allocating.
    ///
    /// Use a valid key: this serializes the supplied coordinates verbatim, while
    /// [`Receiver`] refuses out-of-range coordinates in a response.
    pub fn request(self) -> [u8; REQUEST_BYTES] {
        let mut request = [0; REQUEST_BYTES];
        request[..4].copy_from_slice(MAGIC);
        request[4] = self.zoom.level();
        request[5..9].copy_from_slice(&self.x.to_be_bytes());
        request[9..13].copy_from_slice(&self.y.to_be_bytes());
        request
    }
}

/// One complete, validated tile with an RGB565 palette and encoded scanlines.
///
/// Tile construction is private to the protocol receiver. Pixel storage is
/// bounded by 14,450 bytes in blocks no larger than 1,024 bytes; row offsets and
/// vector metadata are accounted separately. Decoding a row does not allocate.
pub struct Tile {
    pub key: Key,
    pub palette: [u16; 16],
    pixels: Pixels,
}

impl Tile {
    #[cfg(test)]
    pub(crate) fn blank_for_test(key: Key) -> Self {
        let mut pixels = Pixels::allocate().unwrap();
        for _ in 0..SIDE {
            pixels.push(&[0; ROW_BYTES]).unwrap();
        }
        Self {
            key,
            palette: [0; 16],
            pixels,
        }
    }

    /// Decode one row to palette indexes in `0..16`.
    ///
    /// Returns [`io::ErrorKind::InvalidInput`] for a row outside `0..SIDE`,
    /// without changing the output buffer.
    pub fn row(&self, row: usize, colors: &mut [u8; SIDE]) -> io::Result<()> {
        if row >= SIDE {
            return Err(io::ErrorKind::InvalidInput.into());
        }
        self.pixels.row(row, colors);
        Ok(())
    }
}

// Keep validated scanlines in their wire encoding. Small blocks avoid
// requiring a contiguous tile allocation during BLE export and PPP.
struct Pixels {
    blocks: Vec<Vec<u8>>,
    row_ends: Vec<u16>,
}

impl Pixels {
    fn allocate() -> Result<Self, std::collections::TryReserveError> {
        let mut blocks = Vec::new();
        let mut row_ends = Vec::new();
        blocks.try_reserve_exact(BLOCKS)?;
        row_ends.try_reserve_exact(SIDE)?;
        Ok(Self { blocks, row_ends })
    }

    fn push(&mut self, mut bytes: &[u8]) -> Result<(), std::collections::TryReserveError> {
        let mut offset = usize::from(self.row_ends.last().copied().unwrap_or(0));
        while !bytes.is_empty() {
            if offset % BLOCK_BYTES == 0 {
                let mut block = Vec::new();
                block.try_reserve_exact((PIXEL_BYTES - offset).min(BLOCK_BYTES))?;
                self.blocks.push(block);
            }
            let length = bytes.len().min(BLOCK_BYTES - offset % BLOCK_BYTES);
            self.blocks
                .last_mut()
                .unwrap()
                .extend_from_slice(&bytes[..length]);
            offset += length;
            bytes = &bytes[length..];
        }
        self.row_ends.push(offset as u16);
        Ok(())
    }

    fn row(&self, y: usize, colors: &mut [u8; SIDE]) {
        let start = y
            .checked_sub(1)
            .map_or(0, |index| usize::from(self.row_ends[index]));
        let end = usize::from(self.row_ends[y]);
        let bytes =
            (start..end).map(|offset| self.blocks[offset / BLOCK_BYTES][offset % BLOCK_BYTES]);
        if end - start == ROW_BYTES {
            for (pair, byte) in colors.chunks_exact_mut(2).zip(bytes) {
                pair.copy_from_slice(&[byte >> 4, byte & 0x0f]);
            }
        } else {
            let mut offset = 0;
            for run in bytes {
                let next = offset + usize::from(run >> 4) + 1;
                colors[offset..next].fill(run & 0x0f);
                offset = next;
            }
        }
    }
}

struct Scanline {
    bytes: [u8; ROW_BYTES],
    row: usize,
    filled: usize,
}

impl Scanline {
    fn append(&mut self, pixels: &mut Pixels, lengths: &[u8], mut bytes: &[u8]) -> io::Result<()> {
        while !bytes.is_empty() {
            let length = usize::from(lengths[self.row]);
            let count = (length - self.filled).min(bytes.len());
            self.bytes[self.filled..self.filled + count].copy_from_slice(&bytes[..count]);
            self.filled += count;
            bytes = &bytes[count..];
            if self.filled == length {
                let row = &self.bytes[..length];
                if length < ROW_BYTES
                    && row
                        .iter()
                        .map(|run| usize::from(run >> 4) + 1)
                        .sum::<usize>()
                        != SIDE
                {
                    return Err(io::ErrorKind::InvalidData.into());
                }
                pixels.push(row).map_err(|_| io::ErrorKind::OutOfMemory)?;
                self.row += 1;
                self.filled = 0;
            }
        }
        Ok(())
    }
}

/// Incremental decoder for one response at a time, split at any byte boundary.
///
/// Fixed metadata bounds the payload. A poll performs at most one source read,
/// processing at most 512 payload bytes. The caller must supply a nonblocking
/// reader to keep polling nonblocking. No worker or additional queue is created.
///
/// A fatal I/O or protocol error releases partial tile storage and makes this
/// receiver terminal. Replace it with [`Receiver::default`] for a fresh stream;
/// it never guesses the next frame boundary after an error.
pub struct Receiver {
    header: [u8; REQUEST_BYTES + 1],
    received: usize,
    remaining: usize,
    pixels: Option<Pixels>,
    metadata: [u8; METADATA_BYTES],
    scanline: Scanline,
    failed: Option<io::ErrorKind>,
}

impl Default for Receiver {
    fn default() -> Self {
        Self {
            header: [0; REQUEST_BYTES + 1],
            received: 0,
            remaining: 0,
            pixels: None,
            metadata: [0; METADATA_BYTES],
            scanline: Scanline {
                bytes: [0; ROW_BYTES],
                row: 0,
                filled: 0,
            },
            failed: None,
        }
    }
}

/// A complete tile or an explicit unavailable reply from the peer.
pub enum Response {
    Tile(Tile),
    Unavailable(Key),
}

impl Receiver {
    /// Free storage for an obsolete transfer even if the stream is blocked.
    /// Continue draining its remaining bytes before admitting another header.
    pub fn retain(&mut self, wanted: Option<Key>) {
        if self.pixels.is_some() && wanted != self.key() {
            self.pixels = None;
        }
    }

    /// Read one bounded portion of a response.
    ///
    /// Only a response whose key equals `wanted` retains pixel storage. An
    /// obsolete reply is drained to preserve framing. `WouldBlock` and
    /// `Interrupted` report no progress; end of stream is `UnexpectedEof`.
    /// Allocation failure discards and drains that tile, logs a warning, and
    /// leaves retry timing to the caller.
    pub fn poll(
        &mut self,
        source: &mut dyn Read,
        wanted: Option<Key>,
    ) -> io::Result<Option<Response>> {
        if let Some(kind) = self.failed {
            return Err(kind.into());
        }
        let result = self.receive(source, wanted);
        if let Err(error) = &result {
            self.pixels = None;
            self.failed = Some(error.kind());
        }
        result
    }

    fn receive(
        &mut self,
        source: &mut dyn Read,
        wanted: Option<Key>,
    ) -> io::Result<Option<Response>> {
        let end = if self.received < self.header.len() {
            self.received += read(source, &mut self.header[self.received..])?;
            if self.received < self.header.len() {
                return Ok(None);
            }
            let key = self.key().ok_or(io::ErrorKind::InvalidData)?;
            if &self.header[..4] != MAGIC {
                return Err(io::ErrorKind::InvalidData.into());
            }
            match self.header[REQUEST_BYTES] {
                0 => {
                    self.scanline.row = 0;
                    self.scanline.filled = 0;
                    if wanted == Some(key) {
                        self.pixels = Pixels::allocate()
                            .inspect_err(|_| log::warn!("map: insufficient tile storage"))
                            .ok();
                    }
                    return Ok(None);
                }
                1 => Response::Unavailable(key),
                _ => return Err(io::ErrorKind::InvalidData.into()),
            }
        } else {
            self.retain(wanted);
            let offset = self.received - self.header.len();
            if offset < METADATA_BYTES {
                self.received += read(source, &mut self.metadata[offset..])?;
                if self.received == self.header.len() + METADATA_BYTES {
                    let lengths = &self.metadata[PALETTE_BYTES..];
                    if lengths
                        .iter()
                        .any(|&length| length == 0 || usize::from(length) > ROW_BYTES)
                    {
                        return Err(io::ErrorKind::InvalidData.into());
                    }
                    self.remaining = lengths.iter().map(|&length| usize::from(length)).sum();
                }
                return Ok(None);
            }
            let mut buffer = [0; 512];
            let limit = self.remaining.min(buffer.len());
            let count = read(source, &mut buffer[..limit])?;
            self.remaining -= count;
            if let Some(pixels) = &mut self.pixels {
                match self.scanline.append(
                    pixels,
                    &self.metadata[PALETTE_BYTES..],
                    &buffer[..count],
                ) {
                    Ok(()) => {}
                    Err(error) if error.kind() == io::ErrorKind::OutOfMemory => {
                        log::warn!("map: insufficient tile storage");
                        self.pixels = None;
                    }
                    Err(error) => return Err(error),
                }
            }
            if self.remaining != 0 {
                return Ok(None);
            }
            let Some(pixels) = self.pixels.take() else {
                self.received = 0;
                return Ok(None);
            };
            Response::Tile(Tile {
                key: self.key().unwrap(),
                palette: std::array::from_fn(|index| {
                    u16::from_be_bytes([self.metadata[index * 2], self.metadata[index * 2 + 1]])
                }),
                pixels,
            })
        };
        self.received = 0;
        Ok(Some(end))
    }

    fn key(&self) -> Option<Key> {
        let zoom = Zoom::new(self.header[4])?;
        let x = u32::from_be_bytes(self.header[5..9].try_into().unwrap());
        let y = u32::from_be_bytes(self.header[9..13].try_into().unwrap());
        Key::new(zoom, x, y)
    }
}

fn read(source: &mut dyn Read, buffer: &mut [u8]) -> io::Result<usize> {
    match source.read(buffer) {
        Ok(0) => Err(io::ErrorKind::UnexpectedEof.into()),
        Ok(count) if count > buffer.len() => Err(io::ErrorKind::InvalidData.into()),
        Err(error)
            if matches!(
                error.kind(),
                io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
            ) =>
        {
            Ok(0)
        }
        result => result,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tile_key(x: u32, y: u32) -> Key {
        Key {
            zoom: Zoom::new(15).unwrap(),
            x,
            y,
        }
    }

    #[test]
    fn rows_preserve_palette_indexes_in_both_encodings() {
        let mut pixels = Pixels::allocate().unwrap();
        let mut literal = [0x12; ROW_BYTES];
        literal[0] = 0x34;
        pixels.push(&literal).unwrap();
        let repeated = [0xab; ROW_BYTES];
        let mut flat = [0xfa; 11];
        flat[10] = 0x9a;
        pixels.push(&flat).unwrap();
        pixels.push(&repeated).unwrap();
        let mut row = [0; SIDE];
        pixels.row(0, &mut row);
        assert_eq!(&row[..6], &[3, 4, 1, 2, 1, 2]);
        pixels.row(1, &mut row);
        assert_eq!(row, [10; SIDE]);
        pixels.row(2, &mut row);
        assert_eq!(&row[..6], &[10, 11, 10, 11, 10, 11]);
    }

    #[test]
    fn fragmented_wire_only_publishes_complete_tiles_and_checks_keys() {
        let key = Key {
            zoom: Zoom::new(18).unwrap(),
            x: 0x0002_fffe,
            y: 0x0003_ffff,
        };
        assert_eq!(&key.request(), b"NXM5\x12\0\x02\xff\xfe\0\x03\xff\xff");
        let mut receiver = Receiver::default();
        let mut frame = key.request().to_vec();
        frame.push(0);
        frame.extend([0xab; PALETTE_BYTES]);
        frame.extend([ROW_BYTES as u8; SIDE]);
        frame.extend(vec![0xab; PIXEL_BYTES]);
        for part in frame[..frame.len() - 1].chunks(17) {
            let mut data = part;
            while !data.is_empty() {
                assert!(receiver.poll(&mut data, Some(key)).unwrap().is_none());
            }
        }
        let Some(Response::Tile(tile)) = receiver
            .poll(&mut &frame[frame.len() - 1..], Some(key))
            .unwrap()
        else {
            panic!("expected a complete response");
        };
        assert_eq!(tile.key, key);
        assert_eq!(tile.palette, [0xabab; 16]);
        let mut row = [0; SIDE];
        for y in 0..SIDE {
            tile.row(y, &mut row).unwrap();
            assert!(row.chunks_exact(2).all(|pair| pair == [10, 11]));
        }
        let mut unavailable = key.request().to_vec();
        unavailable.push(1);
        assert!(matches!(
            receiver.poll(&mut unavailable.as_slice(), Some(key)).unwrap(),
            Some(Response::Unavailable(received)) if received == key
        ));
        unavailable[10] = 4;
        assert!(receiver
            .poll(&mut unavailable.as_slice(), Some(key))
            .is_err());
    }

    #[test]
    fn rejects_scanlines_with_invalid_lengths_or_pixel_counts() {
        let key = tile_key(1, 2);
        let mut frame = key.request().to_vec();
        frame.push(0);
        frame.extend([0; PALETTE_BYTES]);
        for length in [0, ROW_BYTES as u8 + 1, 1] {
            let mut packet = frame.clone();
            packet.extend([length; SIDE]);
            packet.extend([0xfa; SIDE]);
            let mut data = packet.as_slice();
            let mut receiver = Receiver::default();
            assert!(receiver.poll(&mut data, Some(key)).unwrap().is_none());
            let result = receiver.poll(&mut data, Some(key));
            let invalid = if length == 1 {
                assert!(result.unwrap().is_none());
                receiver.poll(&mut data, Some(key))
            } else {
                result
            };
            assert!(matches!(invalid, Err(error) if error.kind() == io::ErrorKind::InvalidData));
        }
    }

    #[test]
    fn abandoning_a_partial_tile_releases_it_and_preserves_stream_framing() {
        let old = tile_key(1, 2);
        let new = tile_key(3, 4);
        let mut receiver = Receiver::default();
        let mut frame = old.request().to_vec();
        frame.push(0);
        frame.extend([0; PALETTE_BYTES]);
        frame.extend([ROW_BYTES as u8; SIDE]);
        frame.extend(vec![0; PIXEL_BYTES]);
        frame.extend(new.request());
        frame.push(1);
        let mut data = frame.as_slice();
        assert!(receiver.poll(&mut data, Some(old)).unwrap().is_none());
        assert!(receiver.pixels.is_some());
        receiver.retain(Some(new));
        assert!(receiver.pixels.is_none());
        let response = loop {
            if let Some(response) = receiver.poll(&mut data, Some(new)).unwrap() {
                break response;
            }
        };
        assert!(matches!(response, Response::Unavailable(key) if key == new));
        assert!(data.is_empty());
    }

    #[test]
    fn public_row_bounds_preserve_the_output_on_error() {
        let tile = Tile::blank_for_test(tile_key(1, 2));
        let mut colors = [9; SIDE];
        for row in [SIDE, usize::MAX] {
            assert_eq!(
                tile.row(row, &mut colors).unwrap_err().kind(),
                io::ErrorKind::InvalidInput
            );
            assert_eq!(colors, [9; SIDE]);
        }
        tile.row(SIDE - 1, &mut colors).unwrap();
        assert_eq!(colors, [0; SIDE]);
    }

    #[test]
    fn a_fatal_parse_error_releases_storage_and_does_not_guess_a_new_frame() {
        let key = tile_key(1, 2);
        let mut frame = key.request().to_vec();
        frame.push(0);
        frame.extend([0; METADATA_BYTES]); // Zero scanline lengths are invalid.
        let mut input = frame.as_slice();
        let mut receiver = Receiver::default();
        assert!(receiver.poll(&mut input, Some(key)).unwrap().is_none());
        assert!(receiver.pixels.is_some());
        assert_eq!(
            receiver.poll(&mut input, Some(key)).err().unwrap().kind(),
            io::ErrorKind::InvalidData
        );
        assert!(receiver.pixels.is_none());

        let mut next = key.request().to_vec();
        next.push(1);
        let mut input = next.as_slice();
        assert_eq!(
            receiver.poll(&mut input, Some(key)).err().unwrap().kind(),
            io::ErrorKind::InvalidData
        );
        assert_eq!(input, next);
        assert!(matches!(
            Receiver::default().poll(&mut input, Some(key)).unwrap(),
            Some(Response::Unavailable(received)) if received == key
        ));
    }

    #[test]
    fn invalid_reader_counts_cannot_overflow_receiver_offsets() {
        struct InvalidReader;
        impl Read for InvalidReader {
            fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
                Ok(usize::MAX)
            }
        }
        let mut receiver = Receiver::default();
        assert_eq!(
            receiver
                .poll(&mut InvalidReader, None)
                .err()
                .unwrap()
                .kind(),
            io::ErrorKind::InvalidData
        );
        assert_eq!(receiver.received, 0);
    }

    #[test]
    fn nonblocking_reads_preserve_progress_but_eof_is_terminal() {
        struct Unready(io::ErrorKind);
        impl Read for Unready {
            fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
                Err(self.0.into())
            }
        }
        let key = tile_key(1, 2);
        let mut reply = key.request().to_vec();
        reply.push(1);
        let mut receiver = Receiver::default();
        assert!(receiver
            .poll(&mut &reply[..7], Some(key))
            .unwrap()
            .is_none());
        for kind in [io::ErrorKind::WouldBlock, io::ErrorKind::Interrupted] {
            assert!(receiver
                .poll(&mut Unready(kind), Some(key))
                .unwrap()
                .is_none());
            assert_eq!(receiver.received, 7);
        }
        assert!(matches!(
            receiver.poll(&mut &reply[7..], Some(key)).unwrap(),
            Some(Response::Unavailable(received)) if received == key
        ));
        assert_eq!(
            receiver.poll(&mut &[][..], Some(key)).err().unwrap().kind(),
            io::ErrorKind::UnexpectedEof
        );
        let mut next = reply.as_slice();
        assert_eq!(
            receiver.poll(&mut next, Some(key)).err().unwrap().kind(),
            io::ErrorKind::UnexpectedEof
        );
        assert_eq!(next, reply);
    }
}
