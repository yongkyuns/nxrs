//! Bounded camera multipart framing shared with the camera stream writer.
use crate::{Error, Result};
use capture_wire::{AudioBlock, AudioStamp, MicrophoneState, AUDIO_TYPE, AV_HEADER, AV_VERSION, BLOCK_BYTES, CONTROLLED_AV_VERSION, MICROPHONE_STATE_TYPE};
use std::collections::BTreeMap;
use std::num::NonZeroU64;
use tokio::io::{AsyncBufRead, AsyncBufReadExt, AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, BufReader};

pub const MAX_FRAME: usize = 153_600;
const HTTP_LIMIT: usize = 4096;
const PART_LIMIT: usize = 1024;

/// Bounded even when an attacker never sends a newline. Deadlines belong to the caller.
pub async fn line<R: AsyncBufRead + Unpin>(input: &mut R, limit: usize) -> Result<Vec<u8>> {
    let mut result = Vec::new();
    loop {
        let buffer = input.fill_buf().await?;
        if buffer.is_empty() { return Err(Error::Io); }
        let newline = buffer.iter().position(|b| *b == b'\n');
        let count = newline.map_or(buffer.len(), |index| index + 1);
        if result.len() + count > limit { return Err(Error::Protocol); }
        result.extend_from_slice(&buffer[..count]);
        input.consume(count);
        if newline.is_some() { return Ok(result); }
    }
}
async fn headers<R: AsyncBufRead + Unpin>(input: &mut R, cap: usize) -> Result<BTreeMap<String,String>> {
    let mut result = BTreeMap::new();
    let mut remaining = cap;
    loop {
        let bytes = line(input, remaining).await?;
        remaining -= bytes.len();
        if bytes == b"\r\n" { return Ok(result); }
        let text = std::str::from_utf8(&bytes).map_err(|_| Error::Protocol)?;
        let text = text.strip_suffix("\r\n").ok_or(Error::Protocol)?;
        let (name, value) = text.split_once(':').ok_or(Error::Protocol)?;
        if name.is_empty() || !name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
            || !value.bytes().all(|b| (32..=126).contains(&b)) || result.len() >= 16
        { return Err(Error::Protocol); }
        if result.insert(name.to_ascii_lowercase(), value.trim_matches(' ').to_owned()).is_some() {
            return Err(Error::Protocol);
        }
    }
}
fn integer(headers: &BTreeMap<String,String>, name: &str) -> Result<u64> {
    let value = headers.get(name).ok_or(Error::Protocol)?;
    if value.is_empty() || value.len() > 20 || !value.bytes().all(|b| b.is_ascii_digit()) {
        return Err(Error::Protocol);
    }
    value.parse().map_err(|_| Error::Protocol)
}
#[derive(Clone, Copy, Debug)]
pub struct Frame {
    pub length: usize,
    pub sequence: u64,
    /// Device owner clock, not UTC and not sensor exposure time.
    pub capture_ms: u64,
}
#[derive(Clone, Copy)]
pub enum Packet<'a> {
    Video(Frame),
    Audio(AudioBlock<'a>),
    Microphone { state: MicrophoneState, changed: bool },
}
struct Control {
    muted: bool,
    sequence: u64,
    pending: Option<MicrophoneState>,
}
pub struct Reader<R> {
    input: BufReader<R>,
    payload: Vec<u8>,
    last: Option<Frame>,
    audio: bool,
    control: Option<Control>,
    last_audio: Option<AudioStamp>,
    width: u16,
    height: u16,
    pub metadata: Option<detection_wire::Source>,
}
impl<R: AsyncRead + Unpin> Reader<R> {
    pub async fn start(input: R, width: u16, height: u16) -> Result<Self> {
        Self::start_version(input, width, height, None).await
    }

    /// Parse the response to an explicitly requested A/V stream. The caller
    /// must authenticate the transport before issuing that request.
    pub async fn start_av(input: R, width: u16, height: u16) -> Result<Self> {
        Self::start_version(input, width, height, Some(AV_VERSION)).await
    }

    /// A mutually authenticated A/V source starts with capture stopped.
    pub async fn start_controlled(input: R, width: u16, height: u16) -> Result<Self> {
        Self::start_version(input, width, height, Some(CONTROLLED_AV_VERSION)).await
    }

    /// Submit one command after the preceding change has been acknowledged.
    /// Retire the transport on an I/O failure so partial commands are never retried.
    pub async fn request_microphone(&mut self, muted: bool) -> Result<MicrophoneState>
    where
        R: AsyncWrite,
    {
        let control = self.control.as_mut().ok_or(Error::Protocol)?;
        if control.pending.is_some() {
            return Err(Error::Protocol);
        }
        let sequence = control.sequence.checked_add(1).and_then(NonZeroU64::new).ok_or(Error::Protocol)?;
        let state = MicrophoneState { sequence, muted };
        control.pending = Some(state);
        let output = self.input.get_mut();
        output.write_all(&state.encode()).await?;
        output.flush().await?;
        Ok(state)
    }

    pub(crate) fn microphone_muted(&self) -> bool {
        self.control.as_ref().is_some_and(|control| {
            control.muted || control.pending.is_some_and(|change| change.muted)
        })
    }

    pub(crate) fn av_dimensions(&self) -> Option<(u16, u16)> {
        self.audio.then_some((self.width, self.height))
    }

    async fn start_version(input: R, width: u16, height: u16, expected: Option<&str>) -> Result<Self> {
        let mut input = BufReader::with_capacity(8192, input);
        let status = line(&mut input, 128).await?;
        if status != b"HTTP/1.1 200 OK\r\n" { return Err(Error::Protocol); }
        let header = headers(&mut input, HTTP_LIMIT - status.len()).await?;
        if header.get("content-type").map(String::as_str)
            != Some("multipart/x-mixed-replace; boundary=nxrs-frame")
            || ["transfer-encoding", "content-encoding", "content-length"].iter().any(|k| header.contains_key(*k))
        { return Err(Error::Protocol); }
        if header.get(AV_HEADER).map(String::as_str) != expected {
            return Err(Error::Protocol);
        }
        Ok(Self {
            input,
            payload: vec![0; MAX_FRAME],
            last: None,
            audio: expected.is_some(),
            control: (expected == Some(CONTROLLED_AV_VERSION)).then_some(Control {
                muted: true,
                sequence: 0,
                pending: None,
            }),
            last_audio: None,
            width,
            height,
            metadata: None,
        })
    }
    pub async fn next_frame(&mut self) -> Result<Frame> {
        match self.next_packet().await? {
            Packet::Video(frame) => Ok(frame),
            Packet::Audio(_) | Packet::Microphone { .. } => Err(Error::Protocol),
        }
    }

    /// Read one complete part within the caller's deadline. Each stream must
    /// track its own complete-media deadline, even when the other advances.
    pub async fn next_packet(&mut self) -> Result<Packet<'_>> {
        let mut header = self.part_header().await?;
        if header.get("content-type").map(String::as_str) == Some(AUDIO_TYPE) {
            let stamp = self.audio_packet(&header).await?;
            let pcm = self.payload[..BLOCK_BYTES].try_into().expect("fixed PCM length");
            return Ok(Packet::Audio(AudioBlock { stamp, pcm }));
        }
        if header.get("content-type").map(String::as_str) == Some(MICROPHONE_STATE_TYPE) {
            return self.microphone_acknowledgement(&header).await;
        }
        if header.get("content-type").map(String::as_str)
            == Some(detection_wire::METADATA_TYPE)
        {
            if header.contains_key("transfer-encoding") || header.contains_key("content-encoding") {
                return Err(Error::Protocol);
            }
            let length = integer(&header, "content-length")?;
            if length == 0 || length > detection_wire::MAX_SOURCE_BYTES as u64 {
                return Err(Error::Protocol);
            }
            let length = length as usize;
            self.read_payload(length).await?;
            let metadata = detection_wire::Source::decode(&self.payload[..length])
                .map_err(|_| Error::Protocol)?;
            if self.metadata.as_ref().is_some_and(|old| {
                metadata.publication_id <= old.publication_id
                    || metadata.emitted_ms < old.emitted_ms
                    || metadata.event_count < old.event_count
            }) {
                eprintln!("nxrs-camera-ingest: source metadata order regressed");
                return Err(Error::Stale);
            }
            self.metadata = Some(metadata);
            header = self.part_header().await?;
        }
        if header.get("content-type").map(String::as_str) != Some("image/jpeg")
            || header.contains_key("transfer-encoding") || header.contains_key("content-encoding")
        { return Err(Error::Protocol); }
        let length = integer(&header, "content-length")?;
        if !(4..=MAX_FRAME as u64).contains(&length) { return Err(Error::Frame); }
        let frame = Frame { length:length as usize, sequence:integer(&header,"x-sequence")?,
            capture_ms:integer(&header,"x-capture-ms")?,
        };
        if frame.sequence == 0 || self.last.is_some_and(|last| {
                frame.sequence <= last.sequence || frame.capture_ms < last.capture_ms
            })
        {
            eprintln!("nxrs-camera-ingest: source image order regressed");
            return Err(Error::Stale);
        }
        if self.metadata.as_ref().is_some_and(|s| {
            s.latest.as_ref().is_some_and(|o| {
                o.frame_sequence > frame.sequence || o.captured_ms > frame.capture_ms
            })
        })
        {
            eprintln!("nxrs-camera-ingest: source annotation is ahead of its image");
            return Err(Error::Stale);
        }
        self.read_payload(frame.length).await?;
        jpeg_dimensions(&self.payload[..frame.length], self.width, self.height)?;
        self.last = Some(frame);
        Ok(Packet::Video(frame))
    }

    async fn part_header(&mut self) -> Result<BTreeMap<String, String>> {
        if line(&mut self.input, 64).await? != b"--nxrs-frame\r\n" {
            return Err(Error::Protocol);
        }
        headers(&mut self.input, PART_LIMIT).await
    }

    async fn audio_packet(&mut self, header: &BTreeMap<String, String>) -> Result<AudioStamp> {
        if !self.audio
            || self.control.as_ref().is_some_and(|control| control.muted)
            || header.len() != 4
            || integer(header, "content-length")? != BLOCK_BYTES as u64
        {
            return Err(Error::Protocol);
        }
        let stamp = AudioStamp {
            first_sample: integer(header, "x-first-sample")?,
            capture_ms: integer(header, "x-capture-ms")?,
        };
        if stamp.end_sample().is_none() {
            return Err(Error::Protocol);
        }
        if self.last_audio.is_some_and(|previous| !stamp.follows(previous)) {
            return Err(Error::Stale);
        }
        self.read_payload(BLOCK_BYTES).await?;
        self.last_audio = Some(stamp);
        Ok(stamp)
    }

    async fn microphone_acknowledgement(&mut self, header: &BTreeMap<String, String>) -> Result<Packet<'_>> {
        if self.control.as_ref().and_then(|control| control.pending).is_none()
            || header.len() != 2
            || integer(header, "content-length")? != MicrophoneState::BYTES as u64
        {
            return Err(Error::Protocol);
        }
        self.read_payload(MicrophoneState::BYTES).await?;
        let bytes = self.payload[..MicrophoneState::BYTES].try_into().expect("fixed state length");
        let state = MicrophoneState::decode(bytes).ok_or(Error::Protocol)?;
        let control = self.control.as_mut().expect("expected acknowledgement");
        if control.pending != Some(state) {
            return Err(Error::Protocol);
        }
        let changed = control.muted != state.muted;
        if changed {
            self.last_audio = None;
        }
        control.muted = state.muted;
        control.sequence = state.sequence.get();
        control.pending = None;
        Ok(Packet::Microphone { state, changed })
    }

    async fn read_payload(&mut self, length: usize) -> Result<()> {
        // Callers have already checked the maximum size for their media part.
        self.input.read_exact(&mut self.payload[..length]).await?;
        let mut tail = [0; 2];
        self.input.read_exact(&mut tail).await?;
        if tail != *b"\r\n" {
            return Err(Error::Protocol);
        }
        Ok(())
    }

    pub fn payload(&self, frame: Frame) -> &[u8] {
        &self.payload[..frame.length]
    }
}
/// SOF/size checks bound decoder input; FFmpeg must still decode the entropy data.
fn jpeg_dimensions(bytes: &[u8], width: u16, height: u16) -> Result<()> {
    if bytes.len()<4 || !bytes.starts_with(&[0xff,0xd8]) || !bytes.ends_with(&[0xff,0xd9])
        || bytes[2..].windows(2).any(|marker| marker == [0xff,0xd8]) {
        return Err(Error::Frame);
    }
    let mut position=2;
    let mut found=false;
    while position+4 <= bytes.len() {
        if bytes[position]!=0xff { return Err(Error::Frame); }
        let marker=bytes[position+1];
        if marker==0xda { return if found { Ok(()) } else { Err(Error::Frame) }; }
        let length=u16::from_be_bytes([bytes[position+2],bytes[position+3]]) as usize;
        if length<2 || position+2+length>bytes.len() { return Err(Error::Frame); }
        if marker==0xc0 {
            if found || length<8 || bytes[position+4]!=8
                || u16::from_be_bytes([bytes[position+5],bytes[position+6]])!=height
                || u16::from_be_bytes([bytes[position+7],bytes[position+8]])!=width
            { return Err(Error::Frame); }
            found=true;
        } else if (0xc1..=0xcf).contains(&marker) && ![0xc4,0xc8,0xcc].contains(&marker) {
            return Err(Error::Frame); // First profile: 8-bit baseline sensor JPEG only.
        }
        position+=2+length;
    }
    Err(Error::Frame)
}

#[cfg(test)]
mod tests {
    use super::*;
    const RESPONSE:&str="HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=nxrs-frame\r\n\r\n";
    // Header-only fixture: tests framing, NOT a claim that these entropy bytes decode.
    fn jpeg()->Vec<u8> { vec![255,216,255,192,0,11,8,0,240,1,64,1,1,17,0,255,218,0,2,255,217] }
    fn part(sequence:u64, timestamp:u64)->Vec<u8> {
        let payload=jpeg();
        let mut b=format!("--nxrs-frame\r\nContent-Type: image/jpeg\r\nContent-Length: {}\r\nX-Sequence: {sequence}\r\nX-Capture-Ms: {timestamp}\r\n\r\n",payload.len()).into_bytes();
        b.extend(payload); b.extend(b"\r\n"); b
    }
    #[tokio::test] async fn reads_multiple_frames_and_keeps_one_fixed_payload() {
        let mut bytes=RESPONSE.as_bytes().to_vec(); bytes.extend(part(1,100)); bytes.extend(part(3,120));
        let mut r=Reader::start(bytes.as_slice(),320,240).await.unwrap();
        let a=r.next_frame().await.unwrap(); assert_eq!(r.payload(a),jpeg());
        let b=r.next_frame().await.unwrap(); assert_eq!(b.sequence,3); assert_eq!(r.payload.len(),MAX_FRAME);
    }
    #[tokio::test] async fn rejects_redirect_and_transfer_encoding() {
        assert!(Reader::start(b"HTTP/1.1 302 Found\r\n".as_slice(),320,240).await.is_err());
        let response=RESPONSE.replace("\r\n\r\n","\r\nTransfer-Encoding: chunked\r\n\r\n");
        assert!(Reader::start(response.as_bytes(),320,240).await.is_err());
    }
    #[tokio::test] async fn rejects_duplicate_length_and_oversize_before_payload() {
        for extra in ["Content-Length: 2\r\nContent-Length: 3\r\n", "Content-Length: 153601\r\n"] {
            let bytes=format!("{RESPONSE}--nxrs-frame\r\nContent-Type: image/jpeg\r\n{extra}X-Sequence: 1\r\nX-Capture-Ms: 1\r\n\r\n");
            let mut r=Reader::start(bytes.as_bytes(),320,240).await.unwrap();
            assert!(r.next_frame().await.is_err());
        }
    }
    #[tokio::test] async fn rejects_repeated_sequence_and_backwards_clock() {
        for (seq,time) in [(1,200),(2,99)] {
            let mut bytes=RESPONSE.as_bytes().to_vec(); bytes.extend(part(1,100)); bytes.extend(part(seq,time));
            let mut r=Reader::start(bytes.as_slice(),320,240).await.unwrap();
            r.next_frame().await.unwrap(); assert_eq!(r.next_frame().await.unwrap_err(),Error::Stale);
        }
    }
    #[tokio::test] async fn rejects_truncation_and_bad_markers_or_dimensions() {
        let mut bytes=RESPONSE.as_bytes().to_vec(); bytes.extend(part(1,100)); bytes.truncate(bytes.len()-4);
        let mut r=Reader::start(bytes.as_slice(),320,240).await.unwrap(); assert!(r.next_frame().await.is_err());
        assert!(jpeg_dimensions(&jpeg(),640,480).is_err());
        let mut bytes=jpeg(); bytes[0]=0; assert!(jpeg_dimensions(&bytes,320,240).is_err());
    }
    #[tokio::test] async fn no_newline_is_bounded() {
        let bytes=vec![b'a';8192]; let mut r=BufReader::new(bytes.as_slice());
        assert_eq!(line(&mut r,1024).await.unwrap_err(),Error::Protocol);
    }
    #[tokio::test] async fn slow_drip_does_not_reset_whole_frame_deadline() {
        use tokio::{io::AsyncWriteExt, time::{timeout,Duration}};
        let (mut tx,rx)=tokio::io::duplex(8192);
        tx.write_all(RESPONSE.as_bytes()).await.unwrap();
        let mut r=Reader::start(rx,320,240).await.unwrap();
        let task=tokio::spawn(async move {
            for b in part(1,100) { if tx.write_all(&[b]).await.is_err() { break; }
                tokio::time::sleep(Duration::from_millis(10)).await; }
        });
        assert!(timeout(Duration::from_millis(40),r.next_frame()).await.is_err());
        task.abort(); let _=task.await;
    }
}

#[cfg(test)]
mod metadata_tests {
    use super::*;
    #[tokio::test]
    async fn reads_bounded_real_contract_metadata_before_unchanged_jpeg_bytes() {
        let fixture: detection_wire::Snapshot = serde_json::from_slice(include_bytes!(
            "../../../clients/ios/Tests/NxrsMonitorCoreTests/Fixtures/detection.json"
        ))
        .unwrap();
        let source = fixture.source.unwrap();
        let json = serde_json::to_vec(&source).unwrap();
        let jpeg = vec![
            255, 216, 255, 192, 0, 11, 8, 0, 240, 1, 64, 1, 1, 17, 0, 255, 218, 0, 2, 255, 217,
        ];
        let mut wire=b"HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=nxrs-frame\r\n\r\n".to_vec();
        wire.extend(
            format!(
                "--nxrs-frame\r\nContent-Type: {}\r\nContent-Length: {}\r\n\r\n",
                detection_wire::METADATA_TYPE,
                json.len()
            )
            .bytes(),
        );
        wire.extend(&json);
        wire.extend(b"\r\n");
        wire.extend(format!("--nxrs-frame\r\nContent-Type: image/jpeg\r\nContent-Length: {}\r\nX-Sequence: 20\r\nX-Capture-Ms: 1100\r\n\r\n",jpeg.len()).bytes());
        wire.extend(&jpeg);
        wire.extend(b"\r\n");
        let mut reader = Reader::start(wire.as_slice(), 320, 240).await.unwrap();
        let f = reader.next_frame().await.unwrap();
        assert_eq!(reader.payload(f), jpeg);
        assert_eq!(reader.metadata, Some(source));
        let marker = b"X-Sequence: 20";
        let at = wire
            .windows(marker.len())
            .position(|v| v == marker)
            .unwrap();
        let mut bad = wire.clone();
        bad[at + 12] = b'1';
        let mut reader = Reader::start(bad.as_slice(), 320, 240).await.unwrap();
        assert_eq!(reader.next_frame().await.unwrap_err(), Error::Stale);
    }
    #[tokio::test]
    async fn rejects_metadata_oversize_before_reading_the_payload() {
        let wire=format!("HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=nxrs-frame\r\n\r\n--nxrs-frame\r\nContent-Type: {}\r\nContent-Length: 4097\r\n\r\n",detection_wire::METADATA_TYPE);
        let mut reader = Reader::start(wire.as_bytes(), 320, 240).await.unwrap();
        assert_eq!(reader.next_frame().await.unwrap_err(), Error::Protocol);
    }
}
