//! Write a fixed streaming Matroska pipe. The private FFmpeg
//! subprocess owns demuxing and encoding. This module only writes EBML.
use super::Track;
use crate::{Error, Result};
use stream_service::wire::SAMPLE_RATE_HZ;
use tokio::io::{AsyncWrite, AsyncWriteExt};

fn prefix(output: &mut Vec<u8>, tag: u32, length: usize) {
    let tag = tag.to_be_bytes();
    output.extend(&tag[tag.iter().position(|byte| *byte != 0).expect("nonzero tag")..]);
    // The all-ones size value is reserved. Use the shortest ordinary VINT.
    let octets = (1..=8)
        .find(|n| (length as u64) < (1u64 << (7 * n)) - 1)
        .expect("bounded EBML element");
    let size = (length as u64 | 1u64 << (7 * octets)).to_be_bytes();
    output.extend(&size[8 - octets..]);
}

fn element(output: &mut Vec<u8>, tag: u32, bytes: &[u8]) {
    prefix(output, tag, bytes.len());
    output.extend(bytes);
}

fn integer(output: &mut Vec<u8>, tag: u32, value: u64) {
    let value = value.to_be_bytes();
    let start = value.iter().position(|byte| *byte != 0).unwrap_or(7);
    element(output, tag, &value[start..]);
}

fn track(number: u64, kind: u64, codec: &[u8]) -> Vec<u8> {
    let mut entry = Vec::new();
    integer(&mut entry, 0xd7, number); // TrackNumber
    integer(&mut entry, 0x73c5, number); // TrackUID
    integer(&mut entry, 0x83, kind); // TrackType
    integer(&mut entry, 0x9c, 0); // FlagLacing
    element(&mut entry, 0x86, codec); // CodecID
    entry
}

fn header(width: u16, height: u16) -> Vec<u8> {
    let mut header = Vec::new();
    let mut ebml = Vec::new();
    integer(&mut ebml, 0x4286, 1); // EBMLVersion
    integer(&mut ebml, 0x42f7, 1); // EBMLReadVersion
    integer(&mut ebml, 0x42f2, 4); // EBMLMaxIDLength
    integer(&mut ebml, 0x42f3, 8); // EBMLMaxSizeLength
    element(&mut ebml, 0x4282, b"matroska"); // DocType
    integer(&mut ebml, 0x4287, 2); // DocTypeVersion
    integer(&mut ebml, 0x4285, 2); // DocTypeReadVersion
    element(&mut header, 0x1a45dfa3, &ebml); // EBML header
    header.extend([0x18, 0x53, 0x80, 0x67, 0xff]); // Segment of unknown length

    let mut info = Vec::new();
    integer(&mut info, 0x2ad7b1, 1_000_000); // TimestampScale: ns per ms
    element(&mut info, 0x4d80, b"Nxrs"); // MuxingApp
    element(&mut info, 0x5741, b"Nxrs"); // WritingApp
    element(&mut header, 0x1549a966, &info); // Info

    let mut video = track(1, 1, b"V_MJPEG");
    let mut format = Vec::new();
    integer(&mut format, 0xb0, u64::from(width)); // PixelWidth
    integer(&mut format, 0xba, u64::from(height)); // PixelHeight
    element(&mut video, 0xe0, &format); // Video

    let mut audio = track(2, 2, b"A_PCM/INT/LIT");
    format.clear();
    element(&mut format, 0xb5, &f64::from(SAMPLE_RATE_HZ).to_be_bytes()); // SamplingFrequency
    integer(&mut format, 0x9f, 1); // Channels
    integer(&mut format, 0x6264, 16); // BitDepth
    element(&mut audio, 0xe1, &format); // Audio

    let mut tracks = Vec::new();
    element(&mut tracks, 0xae, &video); // TrackEntry
    element(&mut tracks, 0xae, &audio);
    element(&mut tracks, 0xae, &track(3, 17, b"S_TEXT/UTF8"));
    element(&mut header, 0x1654ae6b, &tracks); // Tracks
    header
}

pub(super) struct Writer<W> {
    output: W,
    clock: u64,
    prefix: Vec<u8>,
    cluster: Vec<u8>,
}

impl<W: AsyncWrite + Unpin> Writer<W> {
    pub(super) async fn start(mut output: W, width: u16, height: u16) -> Result<Self> {
        output.write_all(&header(width, height)).await?;
        Ok(Self {
            output,
            clock: 0,
            prefix: Vec::with_capacity(32),
            cluster: Vec::with_capacity(32),
        })
    }

    pub(super) async fn write(&mut self, track: Track, pts: u64, payload: &[u8]) -> Result<()> {
        self.block(0x81 + track as u8, pts, payload).await
    }

    pub(super) async fn write_annotation(&mut self, video_pts: u64, payload: &[u8]) -> Result<()> {
        // Place the private text block one tick ahead of the video in the
        // interleaver. The publisher must match the exact video timestamp.
        self.block(0x83, video_pts.checked_sub(1).ok_or(Error::Stale)?, payload)
            .await
    }

    async fn block(&mut self, track: u8, pts: u64, payload: &[u8]) -> Result<()> {
        // One finite cluster per part. Keep cluster time monotonic while a
        // slightly earlier part from the other track uses a signed offset.
        self.clock = self.clock.max(pts);
        let relative =
            i16::try_from(i128::from(pts) - i128::from(self.clock)).map_err(|_| Error::Stale)?;
        self.cluster.clear();
        integer(&mut self.cluster, 0xe7, self.clock); // Cluster Timestamp
        let block_size = payload.len() + 4;
        if track == 0x83 {
            self.prefix.clear();
            integer(&mut self.prefix, 0x9b, 1); // BlockDuration
            prefix(&mut self.prefix, 0xa1, block_size); // Block
            prefix(&mut self.cluster, 0xa0, self.prefix.len() + block_size); // BlockGroup
            self.cluster.extend(&self.prefix);
        } else {
            prefix(&mut self.cluster, 0xa3, block_size); // SimpleBlock
        }
        self.cluster.push(track);
        self.cluster.extend(relative.to_be_bytes());
        self.cluster.push(if track == 0x83 { 0 } else { 0x80 }); // No lacing
        self.prefix.clear();
        prefix(
            &mut self.prefix,
            0x1f43b675,
            self.cluster.len() + payload.len(),
        );
        self.prefix.extend(&self.cluster);
        self.output.write_all(&self.prefix).await?;
        self.output.write_all(payload).await?;
        Ok(())
    }
}
