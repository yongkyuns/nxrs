//! Exercise the actual camera multipart writer and gateway reader together.
use camera::{Capture, Format, Frame, PixelFormat};
use capture_wire::{
    AudioBlock, AudioStamp, MicrophoneState, AUDIO_TYPE, AV_PATH, BLOCK_BYTES, CONTROLLED_AV_PATH,
};
use nxrs_camera_ingest::{
    av::{encoder, EncoderInput},
    config::Config,
    protocol::{Packet, Reader},
    Error,
};
use std::{
    io::{self, Cursor, Read, Write},
    process::Stdio,
    time::Duration,
};
use stream_service::{HttpSession, Metadata, Options};
use tokio::io::{AsyncReadExt, AsyncWriteExt, DuplexStream};

const JPEG: &[u8] = &[
    255, 216, 255, 192, 0, 11, 8, 0, 240, 1, 64, 1, 1, 17, 0, 255, 218, 0, 2, 255, 217,
];
const PCM: [u8; BLOCK_BYTES] = [0xa5; BLOCK_BYTES];
const RESPONSE: &[u8] =
    b"HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=nxrs-frame\r\n";
const METADATA: &[u8] = br#"{"api_version":"1","publication_id":3,"emitted_ms":100,"presence":"clear","localization":true,"latest":{"frame_sequence":8,"captured_ms":100,"inference_us":0,"person_score_permille":50,"objects":[],"truncated":false},"event_count":0,"events":[]}"#;

struct Transport {
    request: Cursor<Vec<u8>>,
    output: Vec<u8>,
    written: usize,
}

impl Read for Transport {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        match Read::read(&mut self.request, bytes)? {
            0 => Err(io::ErrorKind::WouldBlock.into()),
            count => Ok(count),
        }
    }
}

impl Write for Transport {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        // Yield partway through a part, including inside PCM bytes.
        if self.written >= 350 {
            self.written = 0;
            return Err(io::ErrorKind::WouldBlock.into());
        }
        let count = bytes.len().min(53);
        self.output.extend_from_slice(&bytes[..count]);
        self.written += count;
        Ok(count)
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn transport(path: &str) -> Transport {
    Transport {
        request: Cursor::new(format!("GET {path} HTTP/1.1\r\n\r\n").into_bytes()),
        output: Vec::new(),
        written: 0,
    }
}

fn session(path: &str, authorized: bool) -> HttpSession<Transport, [u8; 64]> {
    let mut http = HttpSession::new([0; 64], Options::default()).unwrap();
    let transport = transport(path);
    if authorized {
        assert!(http.accept_with_audio(transport, 100, &[]));
    } else {
        assert!(http.accept(transport, 100));
    }
    http.poll(100, None);
    assert_eq!(http.audio_requested(), authorized && path == AV_PATH);
    http
}

fn drain(http: &mut HttpSession<Transport, [u8; 64]>, now: u64) {
    for _ in 0..16 {
        if http.ready_for_frame() {
            return;
        }
        assert!(http.poll(now, None).active);
    }
    panic!("bounded test payload did not drain");
}

fn audio(first_sample: u64, capture_ms: u64) -> AudioBlock<'static> {
    AudioBlock {
        stamp: AudioStamp {
            first_sample,
            capture_ms,
        },
        pcm: &PCM,
    }
}

fn frame(sequence: u64, timestamp_ms: u64) -> Frame<'static> {
    Frame {
        sequence,
        capture: Capture {
            format: Format {
                pixels: PixelFormat::Jpeg,
                width: 320,
                height: 240,
            },
            timestamp_ms,
            len: JPEG.len(),
        },
        bytes: JPEG,
    }
}

fn camera_input(http: &mut HttpSession<Transport, [u8; 64]>, input: &[u8]) {
    http.transport_mut()
        .unwrap()
        .request
        .get_mut()
        .extend(input);
}

async fn camera_output(
    http: &mut HttpSession<Transport, [u8; 64]>,
    peer: &mut DuplexStream,
    now: u64,
) {
    drain(http, now);
    let output = std::mem::take(&mut http.transport_mut().unwrap().output);
    peer.write_all(&output).await.unwrap();
}

async fn command(
    reader: &mut Reader<DuplexStream>,
    peer: &mut DuplexStream,
    muted: bool,
) -> MicrophoneState {
    let state = reader.request_microphone(muted).await.unwrap();
    let mut bytes = [0; MicrophoneState::BYTES];
    peer.read_exact(&mut bytes).await.unwrap();
    assert_eq!(state.encode(), bytes);
    assert!(matches!(
        reader.request_microphone(!muted).await,
        Err(Error::Protocol)
    ));
    state
}

#[tokio::test]
async fn controlled_capture_acknowledges_mute_after_retirement_and_resumes_with_fresh_audio() {
    let mut http = HttpSession::new([0; 64], Options::default()).unwrap();
    assert!(http.accept_with_microphone_control(transport(CONTROLLED_AV_PATH), 100, &[]));
    http.poll(100, None);
    let (mut peer, input) = tokio::io::duplex(4096);
    camera_output(&mut http, &mut peer, 100).await;
    let mut reader = Reader::start_controlled(input, 320, 240).await.unwrap();

    let unmute = command(&mut reader, &mut peer, false).await;
    camera_input(&mut http, &unmute.encode()[..2]);
    http.poll(100, None);
    assert_eq!(http.microphone_change(), None);
    camera_input(&mut http, &unmute.encode()[2..]);
    let progress = http.poll_media(101, Some(frame(1, 101)), None, Some(audio(0, 101)));
    assert_eq!(http.microphone_change(), Some(unmute));
    assert!(progress.frame_accepted && !progress.audio_accepted);
    camera_output(&mut http, &mut peer, 101).await;
    assert!(
        matches!(reader.next_packet().await.unwrap(), Packet::Video(frame) if frame.sequence == 1)
    );

    assert!(http.confirm_microphone_state(unmute));
    http.poll(102, None);
    camera_output(&mut http, &mut peer, 102).await;
    assert!(
        matches!(reader.next_packet().await.unwrap(), Packet::Microphone { state, changed: true } if state == unmute)
    );
    assert!(
        http.poll_media(120, None, None, Some(audio(320, 120)))
            .audio_accepted
    );
    assert!(!http.ready_for_frame()); // Part of the old PCM block is on the transport.

    let mute = command(&mut reader, &mut peer, true).await;
    camera_input(&mut http, &mute.encode());
    let old = Some(audio(640, 140));
    assert!(!http.poll_media(140, None, None, old).audio_accepted);
    assert_eq!(http.microphone_change(), Some(mute));
    assert!(!http.confirm_microphone_state(unmute));
    assert!(http.confirm_microphone_state(mute)); // Capture owner joined before confirming.
    drain(&mut http, 140);
    assert!(!http.poll_media(141, None, None, old).audio_accepted);
    camera_output(&mut http, &mut peer, 141).await;
    assert!(
        matches!(reader.next_packet().await.unwrap(), Packet::Audio(block) if block.stamp.first_sample == 320)
    );
    assert!(
        matches!(reader.next_packet().await.unwrap(), Packet::Microphone { state, changed: true } if state == mute)
    );

    let progress = http.poll_media(150, Some(frame(2, 150)), None, old);
    assert!(progress.frame_accepted && !progress.audio_accepted);
    camera_output(&mut http, &mut peer, 150).await;
    assert!(
        matches!(reader.next_packet().await.unwrap(), Packet::Video(frame) if frame.sequence == 2)
    );

    let resume = command(&mut reader, &mut peer, false).await;
    camera_input(&mut http, &resume.encode());
    http.poll(160, None);
    assert!(http.confirm_microphone_state(resume));
    http.poll(161, None);
    drain(&mut http, 161);
    assert!(
        http.poll_media(180, None, None, Some(audio(0, 180)))
            .audio_accepted
    );
    camera_output(&mut http, &mut peer, 180).await;
    assert!(
        matches!(reader.next_packet().await.unwrap(), Packet::Microphone { state, .. } if state == resume)
    );
    assert!(
        matches!(reader.next_packet().await.unwrap(), Packet::Audio(block) if block.stamp == audio(0, 180).stamp)
    );

    camera_input(&mut http, &resume.encode());
    assert!(!http.poll(181, None).active);
}

#[test]
fn controlled_audio_requires_admission_and_cannot_downgrade() {
    for (path, admitted) in [(CONTROLLED_AV_PATH, false), (AV_PATH, true)] {
        let mut http = HttpSession::new([0; 64], Options::default()).unwrap();
        let transport = transport(path);
        if admitted {
            assert!(http.accept_with_microphone_control(transport, 100, &[]));
        } else {
            assert!(http.accept(transport, 100));
        }
        assert!(http.poll(100, None).active);
        assert!(!http.audio_requested());
        assert_eq!(http.microphone_change(), None);
        // The rejection may itself need several short writes.
        for now in 101..108 {
            http.poll(now, None);
        }
        assert!(http.transport_mut().is_none());
        assert_eq!(
            http.stats().last_failure,
            Some(stream_service::Failure::InvalidRequest)
        );
    }
}

#[test]
fn fragmented_microphone_command_has_one_deadline() {
    let mut http = HttpSession::new([0; 64], Options::default()).unwrap();
    assert!(http.accept_with_microphone_control(transport(CONTROLLED_AV_PATH), 100, &[]));
    http.poll(100, None);
    drain(&mut http, 100);
    let command = MicrophoneState {
        sequence: std::num::NonZeroU64::new(1).unwrap(),
        muted: false,
    }
    .encode();
    camera_input(&mut http, &command[..1]);
    assert!(http.poll(100, None).active);
    camera_input(&mut http, &command[1..2]);
    assert!(http.poll(2099, None).active);
    camera_input(&mut http, &command[2..]);
    assert!(!http.poll(2100, None).active);
    assert_eq!(
        http.stats().last_failure,
        Some(stream_service::Failure::RequestDeadline)
    );
}

#[tokio::test]
async fn controlled_reader_refuses_audio_before_an_exact_acknowledgement() {
    for forged_ack in [false, true] {
        let (mut peer, input) = tokio::io::duplex(2048);
        peer.write_all(&response(Some("2"))).await.unwrap();
        let mut reader = Reader::start_controlled(input, 320, 240).await.unwrap();
        let unmute = command(&mut reader, &mut peer, false).await;
        let mut reply = Vec::new();
        if forged_ack {
            let wrong = MicrophoneState {
                muted: true,
                ..unmute
            }
            .encode();
            write!(
                reply,
                "--nxrs-frame\r\nContent-Type: {}\r\nContent-Length: {}\r\n\r\n",
                capture_wire::MICROPHONE_STATE_TYPE,
                wrong.len()
            )
            .unwrap();
            reply.extend(wrong);
            reply.extend(b"\r\n");
        } else {
            write_audio(&mut reply, 0, 100, BLOCK_BYTES);
        }
        peer.write_all(&reply).await.unwrap();
        assert!(matches!(reader.next_packet().await, Err(Error::Protocol)));
    }
}

#[tokio::test]
async fn negotiated_audio_and_annotated_video_survive_short_writes_and_reads() {
    let mut http = session(AV_PATH, true);
    drain(&mut http, 100);
    let metadata = Some(Metadata {
        revision: 3,
        bytes: METADATA,
        content_type: detection_wire::METADATA_TYPE,
    });

    let progress = http.poll_media(100, Some(frame(8, 100)), metadata, Some(audio(0, 100)));
    assert!(progress.audio_accepted);
    assert!(!progress.frame_accepted);
    drain(&mut http, 101);
    assert_eq!(http.stats().frames_completed, 0);
    assert_eq!(http.stats().latest_completed, None);

    let progress = http.poll_media(120, Some(frame(8, 100)), metadata, Some(audio(320, 120)));
    assert!(progress.frame_accepted);
    assert!(!progress.audio_accepted);
    drain(&mut http, 120);
    assert_eq!(http.stats().frames_completed, 1);

    // A skipped block has an explicit 320-sample gap on the wire.
    assert!(
        http.poll_media(140, None, None, Some(audio(640, 140)))
            .audio_accepted
    );
    drain(&mut http, 140);
    assert!(
        !http
            .poll_media(141, None, None, Some(audio(640, 140)))
            .audio_accepted
    );
    assert!(
        !http
            .poll_media(141, None, None, Some(audio(960, 160)))
            .audio_accepted
    );
    assert_eq!(http.stats().frames_completed, 1);

    let output = std::mem::take(&mut http.transport_mut().unwrap().output);
    let (mut sender, receiver) = tokio::io::duplex(19);
    let writer = tokio::spawn(async move {
        for fragment in output.chunks(11) {
            sender.write_all(fragment).await.unwrap();
        }
    });

    let mut reader = Reader::start_av(receiver, 320, 240).await.unwrap();
    let Packet::Audio(block) = reader.next_packet().await.unwrap() else {
        panic!("expected audio packet");
    };
    assert_eq!(block.stamp, audio(0, 100).stamp);
    assert_eq!(block.pcm, &PCM);
    let Packet::Video(frame) = reader.next_packet().await.unwrap() else {
        panic!("expected annotated video");
    };
    assert_eq!((frame.sequence, frame.capture_ms), (8, 100));
    assert_eq!(reader.payload(frame), JPEG);
    assert_eq!(reader.metadata.as_ref().unwrap().publication_id, 3);
    let Packet::Audio(block) = reader.next_packet().await.unwrap() else {
        panic!("expected audio packet");
    };
    assert_eq!(block.stamp, audio(640, 140).stamp);
    assert_eq!(block.pcm, &PCM);
    writer.await.unwrap();
    assert!(matches!(reader.next_packet().await, Err(Error::Io)));
}

#[tokio::test]
async fn legacy_and_unauthorized_transports_do_not_send_audio() {
    let mut legacy = session("/stream", true);
    drain(&mut legacy, 100);
    let progress = legacy.poll_media(100, Some(frame(8, 100)), None, Some(audio(0, 100)));
    assert!(progress.frame_accepted);
    assert!(!progress.audio_accepted);
    drain(&mut legacy, 100);
    let output = &legacy.transport_mut().unwrap().output;
    let mut reader = Reader::start(output.as_slice(), 320, 240).await.unwrap();
    let frame = reader.next_frame().await.unwrap();
    assert_eq!(reader.payload(frame), JPEG);
    assert!(matches!(reader.next_frame().await, Err(Error::Io)));

    struct Reply(Vec<u8>, Cursor<&'static [u8]>);
    impl Read for Reply {
        fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
            Read::read(&mut self.1, bytes)
        }
    }
    impl Write for Reply {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            self.0.extend_from_slice(bytes);
            assert!(self.0.starts_with(b"HTTP/1.1 404 Not Found\r\n"));
            Ok(bytes.len())
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }
    let mut denied = HttpSession::new([0; 64], Options::default()).unwrap();
    assert!(denied.accept(
        Reply(
            Vec::new(),
            Cursor::new(b"GET /stream?av=1 HTTP/1.1\r\n\r\n")
        ),
        100
    ));
    assert!(denied.poll(100, None).active);
    assert!(!denied.audio_requested());
    assert!(
        !denied
            .poll_media(100, None, None, Some(audio(0, 100)))
            .active
    );
    assert_eq!(denied.stats().frames_completed, 0);
}

fn response(version: Option<&str>) -> Vec<u8> {
    let mut bytes = RESPONSE.to_vec();
    if let Some(version) = version {
        write!(bytes, "X-Nxrs-AV: {version}\r\n").unwrap();
    }
    bytes.extend(b"\r\n");
    bytes
}

fn write_audio(output: &mut Vec<u8>, sample: u64, capture_ms: u64, length: usize) {
    write_pcm(output, sample, capture_ms, length, &PCM);
}

fn write_pcm(
    output: &mut Vec<u8>,
    sample: u64,
    capture_ms: u64,
    length: usize,
    pcm: &[u8; BLOCK_BYTES],
) {
    write!(
        output,
        "--nxrs-frame\r\nContent-Type: {AUDIO_TYPE}\r\nContent-Length: {length}\r\nX-First-Sample: {sample}\r\nX-Capture-Ms: {capture_ms}\r\n\r\n",
    ).unwrap();
    output.extend(pcm);
    output.extend(b"\r\n");
}

fn write_jpeg(output: &mut Vec<u8>, sequence: u64, capture_ms: u64, jpeg: &[u8]) {
    write!(output, "--nxrs-frame\r\nContent-Type: image/jpeg\r\nContent-Length: {}\r\nX-Sequence: {sequence}\r\nX-Capture-Ms: {capture_ms}\r\n\r\n", jpeg.len()).unwrap();
    output.extend(jpeg);
    output.extend(b"\r\n");
}

fn encoder_config(timeout_ms: u64) -> Config {
    serde_json::from_value(serde_json::json!({
        "camera": "127.0.0.1:8080", "allow_insecure_camera_http": true,
        "rtsp": "127.0.0.1:8554", "stream_path": "reference", "publisher_user": "fixture",
        "publisher_token_file": "/private/fixture-token", "publisher": "/unused/publisher",
        "ffmpeg": std::env::var("NXRS_TEST_FFMPEG").unwrap_or_else(|_| "/usr/bin/ffmpeg".into()),
        "width": 320, "height": 240, "frame_timeout_ms": timeout_ms,
    }))
    .unwrap()
}

#[tokio::test]
async fn advancing_audio_cannot_extend_the_video_deadline() {
    let (mut writer, source) = tokio::io::duplex(4096);
    let producer = async move {
        writer.write_all(&response(Some("1"))).await.unwrap();
        let mut part = Vec::new();
        for index in 0..100 {
            part.clear();
            write_audio(&mut part, index * 320, index * 20, BLOCK_BYTES);
            if writer.write_all(&part).await.is_err() {
                return;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    };
    let consumer = async move {
        let source = Reader::start_av(source, 320, 240).await.unwrap();
        let mut input = EncoderInput::start(source, tokio::io::sink(), &encoder_config(250))
            .await
            .unwrap();
        tokio::time::timeout(Duration::from_secs(1), async {
            let mut complete = 0;
            loop {
                match input.next().await {
                    Ok(None) => complete += 1,
                    Err(Error::Timeout) => break,
                    other => panic!("unexpected part result: {other:?}"),
                }
            }
            assert!(complete >= 2);
        })
        .await
        .unwrap();
    };
    tokio::join!(producer, consumer);
}

#[tokio::test]
#[ignore = "requires FFmpeg with libx264/libopus and ffprobe; run in media qualification"]
async fn camera_timestamps_and_sample_gaps_survive_the_native_encoder() {
    let config = encoder_config(3000);
    let jpeg = tokio::process::Command::new(&config.ffmpeg)
        .args([
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=white:s=320x240",
            "-frames:v",
            "1",
            "-threads",
            "1",
            "-c:v",
            "mjpeg",
            "-f",
            "image2pipe",
            "pipe:1",
        ])
        .output()
        .await
        .unwrap();
    assert!(jpeg.status.success());

    let mut wire = response(Some("1"));
    let mut parts = 0;
    let mut camera = 1..=8;
    let mut expected_video = Vec::new();
    let mut pcm = [0; BLOCK_BYTES];
    // A 1 kHz square wave on each nonzero block. No household audio or images.
    for (index, sample) in pcm.chunks_exact_mut(2).enumerate() {
        let amplitude = if index % 16 < 8 {
            14_000i16
        } else {
            -14_000i16
        };
        sample.copy_from_slice(&amplitude.to_le_bytes());
    }
    for index in 0..30 {
        let capture_ms = 8_000_000 + 20 * index;
        if !(7..=9).contains(&index) {
            // Arrival jitter in source observations must not stretch the sample clock.
            write_pcm(
                &mut wire,
                (index + 2) * 320,
                capture_ms + index % 3,
                BLOCK_BYTES,
                &pcm,
            );
            parts += 1;
        }
        if index.is_multiple_of(4) {
            let sequence = camera.next().unwrap();
            let offset = 13 - sequence % 3;
            write_jpeg(&mut wire, sequence, capture_ms - offset, &jpeg.stdout);
            expected_video.push(3020 + 20 * index - offset);
            parts += 1;
        }
    }

    let mut child = encoder(&config).spawn().unwrap();
    let source = Reader::start_av(wire.as_slice(), 320, 240).await.unwrap();
    let mut input = EncoderInput::start(source, child.stdin.take().unwrap(), &config)
        .await
        .unwrap();
    let send = async move {
        let mut offered = Vec::new();
        for _ in 0..parts {
            if let Some(picture) = input.next().await.unwrap() {
                assert_eq!(picture.frame.sequence, offered.len() as u64 + 1);
                offered.push(picture.pts_ms);
            }
        }
        offered
    };
    let (encoded, offered) = tokio::time::timeout(Duration::from_secs(10), async {
        tokio::join!(child.wait_with_output(), send)
    })
    .await
    .unwrap();
    let encoded = encoded.unwrap();
    assert!(
        encoded.status.success(),
        "paired encoder failed: {:?}",
        encoded.status.code()
    );
    assert_eq!(offered, expected_video);

    let ffprobe = std::env::var("NXRS_TEST_FFPROBE").unwrap_or_else(|_| "/usr/bin/ffprobe".into());
    let mut probe = tokio::process::Command::new(ffprobe)
        .args([
            "-v",
            "error",
            "-show_packets",
            "-show_streams",
            "-show_entries",
            "stream=codec_name,sample_rate,channels,time_base:packet=codec_type,pts",
            "-of",
            "json",
            "pipe:0",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    let mut pipe = probe.stdin.take().unwrap();
    let read = async move { pipe.write_all(&encoded.stdout).await.unwrap() };
    let (observed, ()) = tokio::time::timeout(Duration::from_secs(10), async {
        tokio::join!(probe.wait_with_output(), read)
    })
    .await
    .unwrap();
    let observed = observed.unwrap();
    assert!(observed.status.success());
    let observed: serde_json::Value = serde_json::from_slice(&observed.stdout).unwrap();
    let streams = observed["streams"].as_array().unwrap();
    assert_eq!(streams.len(), 3);
    assert_eq!(
        (
            streams[0]["codec_name"].as_str(),
            streams[1]["codec_name"].as_str()
        ),
        (Some("h264"), Some("opus"))
    );
    assert_eq!(streams[1]["sample_rate"], "48000");
    assert_eq!(streams[1]["channels"], 1);
    assert_eq!(streams[2]["codec_name"], "subrip");
    assert!(streams.iter().all(|stream| stream["time_base"] == "1/1000"));
    let timestamps = |kind| {
        observed["packets"]
            .as_array()
            .unwrap()
            .iter()
            .filter(move |p| p["codec_type"] == kind)
            .map(|p| p["pts"].as_u64().unwrap())
            .collect::<Vec<_>>()
    };
    assert_eq!(timestamps("video"), offered);
    assert_eq!(
        timestamps("subtitle"),
        offered.iter().map(|ms| ms - 1).collect::<Vec<_>>()
    );
    let mut available = std::collections::VecDeque::new();
    for packet in observed["packets"].as_array().unwrap() {
        match packet["codec_type"].as_str() {
            Some("subtitle") => available.push_back(packet["pts"].as_u64().unwrap() + 1),
            Some("video") => assert_eq!(available.pop_front(), packet["pts"].as_u64()),
            _ => {}
        }
    }
    assert!(available.is_empty());
    let audio = timestamps("audio");
    assert!((27..=28).contains(&audio.len())); // Every block, plus optional encoder tail.
    assert!(audio[0].abs_diff(3014) <= 1); // 6.5 ms Opus priming.
    let differences: Vec<_> = audio.windows(2).map(|pair| pair[1] - pair[0]).collect();
    assert_eq!(
        differences.iter().filter(|d| d.abs_diff(80) <= 1).count(),
        1
    );
    assert_eq!(
        differences.iter().filter(|d| d.abs_diff(20) <= 1).count(),
        differences.len() - 1
    );
}

#[tokio::test]
async fn reader_requires_negotiation_and_rejects_oversized_or_disordered_blocks() {
    for version in [None, Some("2")] {
        assert!(matches!(
            Reader::start_av(response(version).as_slice(), 320, 240).await,
            Err(Error::Protocol)
        ));
    }
    let mut unsolicited = response(None);
    write_audio(&mut unsolicited, 0, 0, BLOCK_BYTES);
    let mut reader = Reader::start(unsolicited.as_slice(), 320, 240)
        .await
        .unwrap();
    assert!(matches!(reader.next_frame().await, Err(Error::Protocol)));

    for (sample, stamp, length, error) in [
        (640, 50, BLOCK_BYTES + 1, Error::Protocol),
        (639, 50, BLOCK_BYTES, Error::Protocol),
        (320, 50, BLOCK_BYTES, Error::Stale),
        (640, 49, BLOCK_BYTES, Error::Stale),
    ] {
        let mut bytes = response(Some("1"));
        write_audio(&mut bytes, 320, 50, BLOCK_BYTES);
        write_audio(&mut bytes, sample, stamp, length);
        let mut reader = Reader::start_av(bytes.as_slice(), 320, 240).await.unwrap();
        assert!(matches!(reader.next_packet().await, Ok(Packet::Audio(_))));
        assert!(matches!(reader.next_packet().await, Err(got) if got == error));
    }
}
