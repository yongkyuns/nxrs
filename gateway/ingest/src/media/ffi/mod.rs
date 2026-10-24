//! Private FFmpeg SDK operations. Generated bindings use the installed headers.
//! No C wrapper, raw POSIX layer, callbacks, worker or reconnect loop is added.
use super::{Failure, Result};
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
}
impl Source {
    pub fn open() -> Result<Self> {
        Self::open_with(
            c"pipe:0",
            c"mpegts",
            &[
                (c"probesize", c"153600"),
                (c"analyzeduration", c"0"),
                (c"max_streams", c"1"),
                (c"protocol_whitelist", c"pipe"),
                (c"codec_whitelist", c""),
            ],
        )
    }
    pub fn rtsp(url: &str) -> Result<Self> {
        let url = zeroize::Zeroizing::new(
            CString::new(url)
                .map_err(|_| Failure::Configuration)?
                .into_bytes_with_nul(),
        );
        let name = CStr::from_bytes_with_nul(&url).map_err(|_| Failure::Configuration)?;
        Self::open_with(
            name,
            c"rtsp",
            &[
                (c"probesize", c"153600"),
                (c"analyzeduration", c"1000000"),
                (c"max_streams", c"1"),
                (c"protocol_whitelist", c"rtsp,tcp"),
                (c"codec_whitelist", c""),
                (c"rtsp_transport", c"tcp"),
                (c"allowed_media_types", c"video"),
                (c"timeout", c"5000000"),
            ],
        )
    }
    fn open_with(url: &CStr, name: &CStr, values: &[(&CStr, &CStr)]) -> Result<Self> {
        let mut source = Self {
            context: ptr::null_mut(),
            packet: ptr::null_mut(),
        };
        let mut options = Options::new(values)?;
        // SAFETY: constant NUL-terminated strings and live dictionary; the SDK
        // allocates/frees a null-initialized context according to its contract.
        unsafe {
            let format = av::av_find_input_format(name.as_ptr());
            if format.is_null()
                || av::avformat_open_input(
                    &mut source.context,
                    url.as_ptr(),
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
                || (*source.context).nb_streams != 1
            {
                return Err(Failure::Media);
            }
            let stream = *(*source.context).streams;
            if stream.is_null() || (*stream).codecpar.is_null() {
                return Err(Failure::Media);
            }
            let parameters = &*(*stream).codecpar;
            if parameters.codec_type != av::AVMediaType::AVMEDIA_TYPE_VIDEO
                || parameters.codec_id != av::AVCodecID::AV_CODEC_ID_H264
                || !(2..=1920).contains(&parameters.width)
                || !(2..=1080).contains(&parameters.height)
                || parameters.width % 2 != 0
                || parameters.height % 2 != 0
                || !(0..=153600).contains(&parameters.extradata_size)
            {
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
            if result < 0
                || (*self.packet).stream_index != 0
                || !(1..=1_048_576).contains(&(*self.packet).size)
            {
                return Err(Failure::Media);
            }
        }
        Ok(true)
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
        let mut options =
            Options::new(&[(c"rtsp_transport", c"tcp"), (c"rw_timeout", c"5000000")])?;
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
            let stream = av::avformat_new_stream(output.context, ptr::null());
            if stream.is_null()
                || av::avcodec_parameters_copy(
                    (*stream).codecpar,
                    (**(*source.context).streams).codecpar,
                ) < 0
            {
                return Err(Failure::Output);
            }
            (*(*stream).codecpar).codec_tag = 0;
            (*stream).time_base = (**(*source.context).streams).time_base;
            if av::avformat_write_header(output.context, &mut options.0) < 0 {
                return Err(Failure::Output);
            }
        }
        Ok(output)
    }
    pub fn write(&mut self, source: &mut Source) -> Result<()> {
        write_packet(self.context, source)
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

/// MPEG-TS stdout owns a native AVIO pipe. The credential stays in the input
/// owner, while its supervising process bounds pipe progress and termination.
pub struct PipeOutput {
    context: *mut av::AVFormatContext,
}
impl PipeOutput {
    pub fn open(source: &Source) -> Result<Self> {
        let mut output = Self {
            context: ptr::null_mut(),
        };
        let mut options = Options::new(&[
            (c"mpegts_flags", c"resend_headers"),
            (c"flush_packets", c"1"),
        ])?;
        // SAFETY: native contexts are exclusive; the SDK copies codec parameters.
        unsafe {
            if av::avformat_alloc_output_context2(
                &mut output.context,
                ptr::null_mut(),
                c"mpegts".as_ptr(),
                c"pipe:1".as_ptr(),
            ) < 0
                || output.context.is_null()
            {
                return Err(Failure::Output);
            }
            let stream = av::avformat_new_stream(output.context, ptr::null());
            if stream.is_null()
                || av::avcodec_parameters_copy(
                    (*stream).codecpar,
                    (**(*source.context).streams).codecpar,
                ) < 0
            {
                return Err(Failure::Output);
            }
            (*(*stream).codecpar).codec_tag = 0;
            (*stream).time_base = (**(*source.context).streams).time_base;
            (*output.context).max_delay = 0;
            if av::avio_open(
                &mut (*output.context).pb,
                c"pipe:1".as_ptr(),
                av::AVIO_FLAG_WRITE,
            ) < 0
                || av::avformat_write_header(output.context, &mut options.0) < 0
            {
                return Err(Failure::Output);
            }
        }
        Ok(output)
    }
    pub fn write(&mut self, source: &mut Source) -> Result<()> {
        write_packet(self.context, source)
    }
    pub fn finish(&mut self) -> Result<()> {
        // SAFETY: header succeeded; single-use owner calls trailer once.
        if unsafe { av::av_write_trailer(self.context) } < 0 {
            return Err(Failure::Output);
        }
        Ok(())
    }
}
impl Drop for PipeOutput {
    fn drop(&mut self) {
        // SAFETY: partially initialized null fields are accepted. The supervisor
        // owns the final process barrier if native flushing blocks.
        unsafe {
            if !self.context.is_null() && !(*self.context).pb.is_null() {
                av::avio_closep(&mut (*self.context).pb);
            }
            av::avformat_free_context(self.context);
        }
    }
}

/// The output owners supply their live native context; source owns the packet.
fn write_packet(output: *mut av::AVFormatContext, source: &mut Source) -> Result<()> {
    // SAFETY: contexts/packet are exclusively owned and initialized. The SDK
    // rescales to the output clock and consumes/unrefs on interleaved write.
    unsafe {
        av::av_packet_rescale_ts(
            source.packet,
            (**(*source.context).streams).time_base,
            (**(*output).streams).time_base,
        );
        (*source.packet).pos = -1;
        if av::av_interleaved_write_frame(output, source.packet) < 0 {
            return Err(Failure::Output);
        }
    }
    Ok(())
}
