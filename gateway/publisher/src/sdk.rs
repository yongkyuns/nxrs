//! Private FFmpeg SDK operations. Generated bindings use the installed headers.
//! No C wrapper, raw POSIX layer, callbacks, worker or reconnect loop is added.
use super::EncodedMedia;
use super::{Failure, Result};
use detection_wire::MAX_VIDEO_BYTES;
use ffmpeg_sys_next as av;
use std::{
    ffi::{CStr, CString},
    ptr,
};

pub fn quiet() {
    unsafe {
        av::av_log_set_level(av::AV_LOG_QUIET);
    }
}
struct Options(*mut av::AVDictionary);
impl Options {
    fn new(values: &[(&CStr, &CStr)]) -> Result<Self> {
        let mut options = Self(ptr::null_mut());
        for (key, value) in values {
            if unsafe { av::av_dict_set(&mut options.0, key.as_ptr(), value.as_ptr(), 0) } < 0 {
                return Err(Failure::Media);
            }
        }
        Ok(options)
    }
}
impl Drop for Options {
    fn drop(&mut self) {
        unsafe {
            av::av_dict_free(&mut self.0);
        }
    }
}
pub struct Source {
    context: *mut av::AVFormatContext,
    packet: *mut av::AVPacket,
    streams: usize,
    audio: Option<i32>,
    annotated: bool,
}

pub enum AnnotatedPacket<'a> {
    Picture(i64),
    Audio,
    Text { pts_ms: i64, body: &'a [u8] },
}

const MAX_PICTURE_BYTES: i32 = 1_048_576;
const OPUS_RTP_SAMPLE_RATE: i32 = 48_000;

impl Source {
    pub fn open(format: EncodedMedia) -> Result<Self> {
        // Preserve MPEG-TS program/header discovery. A zero analyzeduration
        // requests automatic multi-second analysis; explicitly bound it instead.
        // Allow the container demuxer to report one unexpected track so the
        // admission check can reject it even if FFmpeg ignores later tracks.
        // The Opus packets can precede the H.264 SPS. Allow 250 ms of media
        // for paired track discovery under the same input byte budget.
        let (name, probe, limit, duration) = match format {
            EncodedMedia::MpegTs => (c"mpegts", c"153600", c"2", c"1"),
            EncodedMedia::MpegTsAv => (c"mpegts", c"153600", c"3", c"250000"),
            EncodedMedia::MatroskaAv => (c"matroska", c"153600", c"4", c"250000"),
            EncodedMedia::AnnexB => (c"h264", c"32", c"1", c"1"),
        };
        let values = [
            (c"probesize", probe),
            (c"analyzeduration", duration),
            (c"max_streams", limit),
            (c"protocol_whitelist", c"pipe"),
            (c"codec_whitelist", c""),
        ];
        let annotated = format == EncodedMedia::MatroskaAv;
        let audio = annotated || format == EncodedMedia::MpegTsAv;
        let mut source = Self {
            context: ptr::null_mut(),
            packet: ptr::null_mut(),
            streams: 1 + usize::from(audio) + usize::from(annotated),
            audio: None,
            annotated,
        };
        let mut options = Options::new(&values)?;
        // SAFETY: constant NUL-terminated strings and live dictionary; the SDK
        // allocates/frees a null-initialized context according to its contract.
        unsafe {
            let format = av::av_find_input_format(name.as_ptr());
            if format.is_null()
                || av::avformat_open_input(
                    &mut source.context,
                    c"pipe:0".as_ptr(),
                    format,
                    &mut options.0,
                ) < 0
            {
                return Err(Failure::Media);
            }
            if (*source.context).codec_whitelist.is_null()
                || !CStr::from_ptr((*source.context).codec_whitelist)
                    .to_bytes()
                    .is_empty()
            {
                return Err(Failure::Media);
            }
            if av::avformat_find_stream_info(source.context, ptr::null_mut()) < 0
                || (*source.context).nb_streams as usize != source.streams
            {
                return Err(Failure::Media);
            }
            for index in 0..source.streams {
                let stream = *(*source.context).streams.add(index);
                if stream.is_null()
                    || (*stream).codecpar.is_null()
                    || (*stream).time_base.num <= 0
                    || (*stream).time_base.den <= 0
                {
                    return Err(Failure::Media);
                }
                let parameters = &*(*stream).codecpar;
                if annotated && ((*stream).time_base.num != 1 || (*stream).time_base.den != 1000) {
                    return Err(Failure::Media);
                }
                match parameters.codec_type {
                    av::AVMediaType::AVMEDIA_TYPE_VIDEO
                        if parameters.codec_id == av::AVCodecID::AV_CODEC_ID_H264
                            && (2..=1920).contains(&parameters.width)
                            && (2..=1080).contains(&parameters.height)
                            && parameters.width % 2 == 0
                            && parameters.height % 2 == 0
                            && (0..=153600).contains(&parameters.extradata_size) =>
                    {
                        if annotated {
                            // AVCDecoderConfigurationRecord and fixed four-byte NAL lengths.
                            if index != 0
                                || parameters.video_delay != 0
                                || parameters.extradata_size < 7
                                || parameters.extradata.is_null()
                            {
                                return Err(Failure::Media);
                            }
                            let avcc = std::slice::from_raw_parts(parameters.extradata, 7);
                            if avcc[0] != 1 || avcc[4] & 3 != 3 {
                                return Err(Failure::Media);
                            }
                        }
                    }
                    av::AVMediaType::AVMEDIA_TYPE_AUDIO
                        if audio
                            && (!annotated || index == 1)
                            && source.audio.is_none()
                            && parameters.codec_id == av::AVCodecID::AV_CODEC_ID_OPUS
                            && parameters.ch_layout.nb_channels == 1
                            && (parameters.sample_rate == OPUS_RTP_SAMPLE_RATE
                                // FFmpeg 7 reports the ingest encoder's input
                                // sample rate from its live Matroska track.
                                || (annotated && parameters.sample_rate == 16_000))
                            && parameters.extradata_size == 19
                            && !parameters.extradata.is_null() =>
                    {
                        // Version 1, one channel and mapping family zero.
                        let header = std::slice::from_raw_parts(parameters.extradata, 19);
                        if !header.starts_with(b"OpusHead\x01\x01") || header[18] != 0 {
                            return Err(Failure::Media);
                        }
                        source.audio = Some(index as i32);
                    }
                    av::AVMediaType::AVMEDIA_TYPE_SUBTITLE
                        if annotated
                            && index == 2
                            && parameters.codec_id == av::AVCodecID::AV_CODEC_ID_SUBRIP
                            && parameters.extradata_size == 0 => {}
                    _ => return Err(Failure::Media),
                }
            }
            if audio != source.audio.is_some() {
                return Err(Failure::Media);
            }
            source.packet = av::av_packet_alloc();
            if source.packet.is_null() {
                return Err(Failure::Media);
            }
        }
        Ok(source)
    }
    pub fn has_timestamps(&self) -> bool {
        // SAFETY: called only after next yielded a packet owned by this source.
        unsafe {
            (*self.packet).dts != av::AV_NOPTS_VALUE && (*self.packet).pts != av::AV_NOPTS_VALUE
        }
    }
    pub fn next(&mut self) -> Result<bool> {
        // SAFETY: both native allocations remain exclusively owned until Drop.
        unsafe {
            av::av_packet_unref(self.packet);
            let result = av::av_read_frame(self.context, self.packet);
            if result == av::AVERROR_EOF {
                return Ok(false);
            }
            let index = (*self.packet).stream_index;
            let max_packet = if self.annotated && index == 2 {
                MAX_VIDEO_BYTES as i32
            } else if self.audio == Some(index) {
                4096
            } else {
                MAX_PICTURE_BYTES
            };
            if result < 0
                || (*self.context).nb_streams as usize != self.streams
                || !(0..self.streams as i32).contains(&index)
                || !(1..=max_packet).contains(&(*self.packet).size)
                || (*self.packet).data.is_null()
            {
                return Err(Failure::Media);
            }
        }
        Ok(true)
    }

    pub fn annotated_packet(&self) -> Result<AnnotatedPacket<'_>> {
        if !self.annotated {
            return Err(Failure::Media);
        }
        // SAFETY: next validated the packet and this borrow prevents unref.
        unsafe {
            let packet = &*self.packet;
            let missing_picture_dts = packet.stream_index == 0 && packet.dts == av::AV_NOPTS_VALUE;
            if packet.pts < 0 || (packet.pts != packet.dts && !missing_picture_dts) {
                return Err(Failure::Media);
            }
            match packet.stream_index {
                0 => Ok(AnnotatedPacket::Picture(packet.pts)),
                1 => Ok(AnnotatedPacket::Audio),
                2 if packet.duration == 1 => Ok(AnnotatedPacket::Text {
                    pts_ms: packet.pts,
                    body: std::slice::from_raw_parts(packet.data, packet.size as usize),
                }),
                _ => Err(Failure::Media),
            }
        }
    }

    pub fn insert_sei(&mut self, sei: &[u8]) -> Result<()> {
        let nal = sei.strip_prefix(&[0, 0, 0, 1]).ok_or(Failure::Media)?;
        // SAFETY: next established a bounded nonempty packet. av_grow_packet
        // may change its storage, so no slice into the original is kept across it.
        unsafe {
            let before = (*self.packet).size as usize;
            let offset = super::annotations::insertion_point(std::slice::from_raw_parts(
                (*self.packet).data,
                before,
            ))?;
            if before + sei.len() > MAX_PICTURE_BYTES as usize
                || av::av_packet_make_writable(self.packet) < 0
                || av::av_grow_packet(self.packet, sei.len() as i32) < 0
            {
                return Err(Failure::Media);
            }
            let data = std::slice::from_raw_parts_mut((*self.packet).data, before + sei.len());
            data.copy_within(offset..before, offset + sei.len());
            data[offset..offset + 4].copy_from_slice(&(nal.len() as u32).to_be_bytes());
            data[offset + 4..offset + sei.len()].copy_from_slice(nal);
        }
        Ok(())
    }
}
impl Drop for Source {
    fn drop(&mut self) {
        // SAFETY: each SDK allocation is owned exactly once; null is accepted.
        unsafe {
            av::av_packet_free(&mut self.packet);
            av::avformat_close_input(&mut self.context);
        }
    }
}
pub struct Publication {
    context: *mut av::AVFormatContext,
}
impl Publication {
    pub fn open(url: &str, source: &Source) -> Result<Self> {
        let url = zeroize::Zeroizing::new(
            CString::new(url)
                .map_err(|_| Failure::Configuration)?
                .into_bytes_with_nul(),
        );
        let mut output = Self {
            context: ptr::null_mut(),
        };
        // Keep annotated access units below MediaMTX's RTP packet ceiling.
        // Its switch to repacketization midway through a large access unit can
        // otherwise resend that unit's already forwarded leading NALs.
        let packet_size = if source.annotated { c"1200" } else { c"1472" };
        let mut options = Options::new(&[
            (c"rtsp_transport", c"tcp"),
            (c"rw_timeout", c"5000000"),
            (c"pkt_size", packet_size),
        ])?;
        // SAFETY: source remains borrowed/alive throughout parameter copying.
        // RTSP is AVFMT_NOFILE: do not call avio_open on an RTSP output URL.
        unsafe {
            if av::avformat_alloc_output_context2(
                &mut output.context,
                ptr::null_mut(),
                c"rtsp".as_ptr(),
                url.as_ptr().cast(),
            ) < 0
                || output.context.is_null()
            {
                return Err(Failure::Output);
            }
            for index in 0..source.streams - usize::from(source.annotated) {
                let input = *(*source.context).streams.add(index);
                let stream = av::avformat_new_stream(output.context, ptr::null());
                if stream.is_null()
                    || av::avcodec_parameters_copy((*stream).codecpar, (*input).codecpar) < 0
                {
                    return Err(Failure::Output);
                }
                (*(*stream).codecpar).codec_tag = 0;
                if source.audio == Some(index as i32) {
                    // Opus over RTP always uses the 48 kHz clock (RFC 7587).
                    // Keep the input time base and rescale the original packets.
                    (*(*stream).codecpar).sample_rate = OPUS_RTP_SAMPLE_RATE;
                }
                (*stream).time_base = (*input).time_base;
            }
            if av::avformat_write_header(output.context, &mut options.0) < 0 {
                return Err(Failure::Output);
            }
        }
        Ok(output)
    }
    pub fn time_base(&self) -> Result<(u32, u32)> {
        // SAFETY: raw Annex-B always has exactly one initialized output stream.
        let value = unsafe { (**(*self.context).streams).time_base };
        if value.num <= 0 || value.den <= 0 {
            return Err(Failure::Output);
        }
        Ok((value.num as u32, value.den as u32))
    }
    pub fn write(&mut self, source: &mut Source, timestamp: Option<i64>) -> Result<()> {
        write_packet(self.context, source, timestamp)
    }
    pub fn finish(&mut self) -> Result<()> {
        // SAFETY: write_header succeeded; finish is called once by the owner.
        if unsafe { av::av_write_trailer(self.context) } < 0 {
            return Err(Failure::Output);
        }
        Ok(())
    }
}
impl Drop for Publication {
    fn drop(&mut self) {
        // No retry or blocking network workflow in Drop. This is a single-use
        // process; its exit is the final descriptor/lifetime barrier on failure.
        unsafe {
            av::avformat_free_context(self.context);
        }
    }
}

/// The output owners supply their live native context; source owns the packet.
fn write_packet(
    output: *mut av::AVFormatContext,
    source: &mut Source,
    timestamp: Option<i64>,
) -> Result<()> {
    // SAFETY: contexts/packet are exclusively owned and initialized. The SDK
    // rescales to the output clock and consumes/unrefs on interleaved write.
    unsafe {
        let index = (*source.packet).stream_index as usize; // Validated by Source::next.
        if source.annotated && index == 0 && (*source.packet).dts == av::AV_NOPTS_VALUE {
            // The live Matroska demuxer can leave decode time unset for the
            // first AVC picture. The admitted encoder has no reordering and
            // its exact presentation time was already matched to the frame.
            (*source.packet).dts = (*source.packet).pts;
        }
        av::av_packet_rescale_ts(
            source.packet,
            (**(*source.context).streams.add(index)).time_base,
            (**(*output).streams.add(index)).time_base,
        );
        // Rust supplies an optional receipt time in this exact output clock.
        // Container/RTP inputs retain their native clock without replacement.
        if let Some(timestamp) = timestamp {
            (*source.packet).pts = timestamp;
            (*source.packet).dts = timestamp;
            (*source.packet).duration = 0;
        }
        (*source.packet).pos = -1;
        if av::av_interleaved_write_frame(output, source.packet) < 0 {
            return Err(Failure::Output);
        }
    }
    Ok(())
}
