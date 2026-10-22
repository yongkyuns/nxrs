#![forbid(unsafe_code)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DeviceError {
    Configuration,
    Frame,
    Model,
    Unsupported,
}
/// Quantized confidence, not a calibrated probability of an intrusion.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Score(u16);
impl Score {
    pub fn new(permille: u16) -> core::result::Result<Self, DeviceError> {
        if permille > 1000 {
            Err(DeviceError::Model)
        } else {
            Ok(Self(permille))
        }
    }
    pub fn permille(self) -> u16 {
        self.0
    }
}
/// Borrowed baseline JPEG. The detector completes synchronously and retains no
/// image references. A service supplies owned copies when dispatching a worker.
#[derive(Clone, Copy, Debug)]
pub struct Image<'a> {
    pub bytes: &'a [u8],
    pub width: u16,
    pub height: u16,
}
impl<'a> Image<'a> {
    pub fn jpeg(bytes: &'a [u8], width: u16, height: u16) -> Self {
        Self {
            bytes,
            width,
            height,
        }
    }
}
/// Normalized coordinates 0..=1000, relative to the entire source image.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Bounds {
    pub left: u16,
    pub top: u16,
    pub right: u16,
    pub bottom: u16,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Detection {
    pub score: Score,
    pub bounds: Bounds,
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Detections {
    pub boxes: [Option<Detection>; 10],
    pub truncated: bool,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DetectionResult {
    pub person_score: Score,
    pub objects: Detections,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Capabilities {
    pub localization: bool,
    pub max_detections: usize,
    pub max_width: u16,
    pub max_height: u16,
    pub max_jpeg_bytes: usize,
    pub model_width: u16,
    pub model_height: u16,
}
impl Capabilities {
    pub fn presence(model_width: u16, model_height: u16) -> Self {
        Self {
            localization: false,
            max_detections: 0,
            max_width: 640,
            max_height: 480,
            max_jpeg_bytes: 153600,
            model_width,
            model_height,
        }
    }
    pub fn localized(w: u16, h: u16) -> Self {
        Self {
            localization: true,
            max_detections: 10,
            ..Self::presence(w, h)
        }
    }
}
#[derive(Clone, Copy, Debug)]
pub struct Config {
    pub require_localization: bool,
    pub box_threshold: Score,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            require_localization: false,
            box_threshold: Score(500),
        }
    }
}
/// Capability-owned, portable synchronous inference. The caller owns scheduling,
/// deadlines and alert policy; errors never imply an empty/clear observation.
pub trait Detector: Send {
    fn capabilities(&self) -> Capabilities;
    fn detect(&mut self, image: Image<'_>) -> core::result::Result<DetectionResult, DeviceError>;
}

impl Detections {
    pub fn iter(&self) -> impl Iterator<Item = &Detection> {
        self.boxes.iter().flatten()
    }
    pub fn len(&self) -> usize {
        self.iter().count()
    }
    pub fn is_empty(&self) -> bool {
        self.iter().next().is_none()
    }
}

impl core::fmt::Display for DeviceError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.write_str(match self {
            Self::Configuration => "invalid detector configuration",
            Self::Frame => "invalid or unsupported image",
            Self::Model => "detector inference failed",
            Self::Unsupported => "requested detector capability is unavailable",
        })
    }
}
impl core::error::Error for DeviceError {}

impl DetectionResult {
    pub fn presence(person_score: Score) -> Self {
        Self {
            person_score,
            objects: Detections::default(),
        }
    }
}
