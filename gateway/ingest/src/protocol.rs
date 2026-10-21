//! The current service/stream wire contract, NOT a generic Internet MJPEG client.
use crate::{Error, Result};
use std::collections::BTreeMap;
use tokio::io::{AsyncBufRead, AsyncBufReadExt, AsyncRead, AsyncReadExt, BufReader};

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
pub struct Reader<R> {
    input: BufReader<R>,
    payload: Vec<u8>,
    last: Option<Frame>,
    width: u16,
    height: u16,
}
impl<R: AsyncRead + Unpin> Reader<R> {
    pub async fn start(input: R, width: u16, height: u16) -> Result<Self> {
        let mut input = BufReader::with_capacity(8192, input);
        let status = line(&mut input, 128).await?;
        if status != b"HTTP/1.1 200 OK\r\n" { return Err(Error::Protocol); }
        let header = headers(&mut input, HTTP_LIMIT - status.len()).await?;
        if header.get("content-type").map(String::as_str)
            != Some("multipart/x-mixed-replace; boundary=nxrs-frame")
            || ["transfer-encoding", "content-encoding", "content-length"].iter().any(|k| header.contains_key(*k))
        { return Err(Error::Protocol); }
        Ok(Self { input, payload: vec![0; MAX_FRAME], last: None, width, height })
    }
    pub async fn next_frame(&mut self) -> Result<Frame> {
        if line(&mut self.input, 64).await? != b"--nxrs-frame\r\n" { return Err(Error::Protocol); }
        let header = headers(&mut self.input, PART_LIMIT).await?;
        if header.get("content-type").map(String::as_str) != Some("image/jpeg")
            || header.contains_key("transfer-encoding") || header.contains_key("content-encoding")
        { return Err(Error::Protocol); }
        let length = integer(&header, "content-length")?;
        if !(4..=MAX_FRAME as u64).contains(&length) { return Err(Error::Frame); }
        let frame = Frame { length:length as usize, sequence:integer(&header,"x-sequence")?,
            capture_ms:integer(&header,"x-capture-ms")? };
        if frame.sequence == 0 || self.last.is_some_and(|last|
            frame.sequence <= last.sequence || frame.capture_ms < last.capture_ms)
        { return Err(Error::Stale); }
        self.input.read_exact(&mut self.payload[..frame.length]).await?;
        let mut tail = [0;2];
        self.input.read_exact(&mut tail).await?;
        if tail != *b"\r\n" { return Err(Error::Protocol); }
        jpeg_dimensions(&self.payload[..frame.length], self.width, self.height)?;
        self.last = Some(frame);
        Ok(frame)
    }
    pub fn payload(&self, frame: Frame) -> &[u8] { &self.payload[..frame.length] }
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
