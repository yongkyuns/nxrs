//! Bounded tile request and reply protocol for the phone and IP relay.
use std::{
    io::{self, Read},
    sync::{Arc, Mutex},
};

pub const SIDE: usize = 170;
const PALETTE_BYTES: usize = 32;
const ROW_BYTES: usize = SIDE / 2;
const PIXEL_BYTES: usize = ROW_BYTES * SIDE;
const METADATA_BYTES: usize = PALETTE_BYTES + SIDE;
const BLOCK_BYTES: usize = 1024;
const BLOCKS_PER_TILE: usize = PIXEL_BYTES.div_ceil(BLOCK_BYTES);
const MAP_BLOCKS: usize = 2 * BLOCKS_PER_TILE;
// Four visible tiles and one incoming transfer.
const MAP_TILES: usize = 5;
const MAGIC: &[u8; 4] = b"NXM5";
pub const REQUEST_BYTES: usize = 13;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Zoom(u8);

impl Zoom {
    pub fn new(level: u8) -> Option<Self> {
        (15..=18).contains(&level).then_some(Self(level))
    }

    pub fn level(self) -> u8 {
        self.0
    }

    pub fn tiles(self) -> u32 {
        1 << self.level()
    }
}

impl Default for Zoom {
    fn default() -> Self {
        Self(18)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Key {
    pub zoom: Zoom,
    pub x: u32,
    pub y: u32,
}

impl Key {
    pub fn request(self) -> [u8; REQUEST_BYTES] {
        let mut request = [0; REQUEST_BYTES];
        request[..4].copy_from_slice(MAGIC);
        request[4] = self.zoom.level();
        request[5..9].copy_from_slice(&self.x.to_be_bytes());
        request[9..13].copy_from_slice(&self.y.to_be_bytes());
        request
    }
}

pub struct Tile {
    pub key: Key,
    pub palette: [u16; 16],
    pixels: Pixels,
}

impl Tile {
    #[cfg(test)]
    pub(crate) fn blank_for_test(key: Key) -> Self {
        let mut pixels = Pool::new().unwrap().take(PIXEL_BYTES).unwrap();
        for _ in 0..SIDE {
            pixels.push(&[0; ROW_BYTES]);
        }
        Self {
            key,
            palette: [0; 16],
            pixels,
        }
    }

    pub fn row(&self, row: usize, colors: &mut [u8; SIDE]) {
        self.pixels.row(row, colors);
    }
}

/// One bounded budget for the incoming tile and everything the screen retains.
#[derive(Clone)]
pub struct Pool(Arc<Mutex<Free>>);

struct Free {
    blocks: Vec<Box<[u8]>>,
    tiles: Vec<Box<TileStorage>>,
}

impl Pool {
    pub fn new() -> io::Result<Self> {
        let mut blocks = nxrs_buffer::try_vec("tracker map block index", MAP_BLOCKS)?;
        for _ in 0..MAP_BLOCKS {
            blocks.push(
                nxrs_buffer::try_zeroed("tracker map block", BLOCK_BYTES)?.into_boxed_slice(),
            );
        }
        let mut tiles = nxrs_buffer::try_vec("tracker map tile index", MAP_TILES)?;
        for _ in 0..MAP_TILES {
            tiles.push(Box::new(TileStorage {
                blocks: [const { None }; BLOCKS_PER_TILE],
                row_ends: [0; SIDE],
                rows: 0,
            }));
        }
        let pool = Self(Arc::new(Mutex::new(Free { blocks, tiles })));
        // Initialize the native mutex while the startup heap is available.
        {
            let _ready = pool.0.lock().unwrap();
        }
        Ok(pool)
    }

    fn take(&self, encoded_bytes: usize) -> Option<Pixels> {
        debug_assert!(encoded_bytes <= PIXEL_BYTES);
        let count = encoded_bytes.div_ceil(BLOCK_BYTES);
        let mut free = self.0.lock().unwrap();
        if free.blocks.len() < count {
            return None;
        }
        let mut storage = free.tiles.pop()?;
        for slot in &mut storage.blocks[..count] {
            *slot = free.blocks.pop();
        }
        Some(Pixels {
            pool: self.clone(),
            storage: Some(storage),
        })
    }
}

// These blocks are exclusively owned by a transfer or its published tile.
struct Pixels {
    pool: Pool,
    storage: Option<Box<TileStorage>>,
}

struct TileStorage {
    blocks: [Option<Box<[u8]>>; BLOCKS_PER_TILE],
    row_ends: [u16; SIDE],
    rows: usize,
}

impl Drop for Pixels {
    fn drop(&mut self) {
        let mut tile = self.storage.take().unwrap();
        let mut free = self.pool.0.lock().unwrap();
        for block in &mut tile.blocks {
            if let Some(block) = block.take() {
                free.blocks.push(block);
            }
        }
        tile.rows = 0;
        free.tiles.push(tile);
    }
}

impl Pixels {
    fn push(&mut self, bytes: &[u8]) {
        self.storage.as_mut().unwrap().push(bytes);
    }

    fn row(&self, y: usize, colors: &mut [u8; SIDE]) {
        self.storage.as_ref().unwrap().row(y, colors);
    }
}

impl TileStorage {
    fn push(&mut self, mut bytes: &[u8]) {
        let mut offset = self
            .rows
            .checked_sub(1)
            .map_or(0, |row| usize::from(self.row_ends[row]));
        while !bytes.is_empty() {
            let start = offset % BLOCK_BYTES;
            let length = bytes.len().min(BLOCK_BYTES - start);
            self.blocks[offset / BLOCK_BYTES].as_mut().unwrap()[start..start + length]
                .copy_from_slice(&bytes[..length]);
            offset += length;
            bytes = &bytes[length..];
        }
        self.row_ends[self.rows] = offset as u16;
        self.rows += 1;
    }

    fn row(&self, y: usize, colors: &mut [u8; SIDE]) {
        let start = y
            .checked_sub(1)
            .map_or(0, |index| usize::from(self.row_ends[index]));
        let end = usize::from(self.row_ends[y]);
        let bytes = (start..end).map(|offset| {
            self.blocks[offset / BLOCK_BYTES].as_ref().unwrap()[offset % BLOCK_BYTES]
        });
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
                pixels.push(row);
                self.row += 1;
                self.filled = 0;
            }
        }
        Ok(())
    }
}

/// One response can be split at any byte. Fixed metadata bounds the payload.
pub struct Receiver {
    pool: Option<Pool>,
    header: [u8; REQUEST_BYTES + 1],
    received: usize,
    remaining: usize,
    wanted: bool,
    pixels: Option<Pixels>,
    metadata: [u8; METADATA_BYTES],
    scanline: Scanline,
}

impl Receiver {
    pub fn new(pool: Option<Pool>) -> Self {
        Self {
            pool,
            header: [0; REQUEST_BYTES + 1],
            received: 0,
            remaining: 0,
            wanted: false,
            pixels: None,
            metadata: [0; METADATA_BYTES],
            scanline: Scanline {
                bytes: [0; ROW_BYTES],
                row: 0,
                filled: 0,
            },
        }
    }
}

pub enum Response {
    Tile(Tile),
    Unavailable(Key),
}

impl Receiver {
    pub fn reset(&mut self) {
        self.received = 0;
        self.pixels = None;
        self.wanted = false;
    }

    /// Free storage for an obsolete transfer even if the stream is blocked.
    /// Continue draining its remaining bytes before admitting another header.
    pub fn retain(&mut self, wanted: Option<Key>) {
        if self.wanted && wanted != self.key() {
            self.wanted = false;
            self.pixels = None;
        }
    }

    pub fn poll(
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
                    // Borrow exactly the blocks needed once all row lengths are known.
                    self.wanted = wanted == Some(key);
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
                    if self.wanted {
                        self.pixels = self
                            .pool
                            .as_ref()
                            .and_then(|pool| pool.take(self.remaining));
                    }
                }
                return Ok(None);
            }
            let mut buffer = [0; 512];
            let limit = self.remaining.min(buffer.len());
            let count = read(source, &mut buffer[..limit])?;
            self.remaining -= count;
            if let Some(pixels) = &mut self.pixels {
                self.scanline
                    .append(pixels, &self.metadata[PALETTE_BYTES..], &buffer[..count])?;
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
        (x < zoom.tiles() && y < zoom.tiles()).then_some(Key { zoom, x, y })
    }
}

fn read(source: &mut dyn Read, buffer: &mut [u8]) -> io::Result<usize> {
    match source.read(buffer) {
        Ok(0) => Err(io::ErrorKind::UnexpectedEof.into()),
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
        let mut literal = [0x12; ROW_BYTES];
        literal[0] = 0x34;
        let repeated = [0xab; ROW_BYTES];
        let mut flat = [0xfa; 11];
        flat[10] = 0x9a;
        let encoded_bytes = literal.len() + flat.len() + repeated.len();
        let pool = Pool::new().unwrap();
        let mut pixels = pool.take(encoded_bytes).unwrap();
        assert_eq!(pool.0.lock().unwrap().blocks.len(), MAP_BLOCKS - 1);
        pixels.push(&literal);
        pixels.push(&flat);
        pixels.push(&repeated);
        let mut row = [0; SIDE];
        pixels.row(0, &mut row);
        assert_eq!(&row[..6], &[3, 4, 1, 2, 1, 2]);
        pixels.row(1, &mut row);
        assert_eq!(row, [10; SIDE]);
        pixels.row(2, &mut row);
        assert_eq!(&row[..6], &[10, 11, 10, 11, 10, 11]);
    }

    #[test]
    fn map_exhaustion_keeps_other_tiles_intact_and_reuses_returned_blocks() {
        let pool = Pool::new().unwrap();
        let first = pool.take(PIXEL_BYTES).unwrap();
        let mut second = pool.take(PIXEL_BYTES).unwrap();
        second.push(&[0x12; ROW_BYTES]);
        assert!(pool.take(1).is_none());
        drop(first);
        let third = pool.take(PIXEL_BYTES).unwrap();
        assert!(pool.take(1).is_none());
        let mut row = [0; SIDE];
        second.row(0, &mut row);
        assert!(row.chunks_exact(2).all(|pair| pair == [1, 2]));
        drop((second, third));
        let free = pool.0.lock().unwrap();
        assert_eq!(free.blocks.len(), MAP_BLOCKS);
        assert_eq!(free.tiles.len(), MAP_TILES);
    }

    #[test]
    fn fragmented_wire_only_publishes_complete_tiles_and_checks_keys() {
        let key = Key {
            zoom: Zoom::new(18).unwrap(),
            x: 0x0002_fffe,
            y: 0x0003_ffff,
        };
        assert_eq!(&key.request(), b"NXM5\x12\0\x02\xff\xfe\0\x03\xff\xff");
        let mut receiver = Receiver::new(Some(Pool::new().unwrap()));
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
            tile.row(y, &mut row);
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
            let mut receiver = Receiver::new(Some(Pool::new().unwrap()));
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
        let mut receiver = Receiver::new(Some(Pool::new().unwrap()));
        let mut frame = old.request().to_vec();
        frame.push(0);
        frame.extend([0; PALETTE_BYTES]);
        frame.extend([ROW_BYTES as u8; SIDE]);
        frame.extend(vec![0; PIXEL_BYTES]);
        frame.extend(new.request());
        frame.push(1);
        let mut data = frame.as_slice();
        assert!(receiver.poll(&mut data, Some(old)).unwrap().is_none());
        assert!(receiver.pixels.is_none());
        assert!(receiver.poll(&mut data, Some(old)).unwrap().is_none());
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
}
