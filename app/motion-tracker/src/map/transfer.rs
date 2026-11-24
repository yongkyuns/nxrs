use super::{Key, Pixels, ROW_BYTES, SIDE, Tile, Zoom};
use std::io::{self, Read};

pub(super) const PALETTE_BYTES: usize = 32;
const METADATA_BYTES: usize = PALETTE_BYTES + SIDE;
pub(super) const MAGIC: &[u8; 4] = b"NXM5";
pub const REQUEST_BYTES: usize = 13;

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

/// One response can be split at any byte. Fixed metadata bounds the payload.
pub struct Receiver {
    header: [u8; REQUEST_BYTES + 1],
    received: usize,
    remaining: usize,
    pixels: Option<Pixels>,
    metadata: [u8; METADATA_BYTES],
    scanline: Scanline,
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
        }
    }
}

pub enum Response {
    Tile(Tile),
    Unavailable(Key),
}

impl Receiver {
    #[cfg(test)]
    pub(super) fn retains_pixels(&self) -> bool {
        self.pixels.is_some()
    }

    /// Free storage for an obsolete transfer even if the stream is blocked.
    /// Continue draining its remaining bytes before admitting another header.
    pub fn retain(&mut self, wanted: Option<Key>) {
        if self.pixels.is_some() && wanted != self.key() {
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
                    if wanted == Some(key) {
                        self.pixels = Pixels::allocate()
                            .inspect_err(|_| {
                                eprintln!("motion-tracker map: insufficient tile storage")
                            })
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
                        eprintln!("motion-tracker map: insufficient tile storage");
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
