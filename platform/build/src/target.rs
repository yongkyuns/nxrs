//! Supported target families; product/qualification configurations are separate.
use crate::Result;
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Target {
    Wroom,
    CoreS3,
    Pico2W,
    Linux,
    Macos,
    Windows,
    Web,
}
impl Target {
    pub const ALL: [Self; 7] = [
        Self::Wroom,
        Self::CoreS3,
        Self::Pico2W,
        Self::Linux,
        Self::Macos,
        Self::Windows,
        Self::Web,
    ];
    pub fn name(self) -> &'static str {
        match self {
            Self::Wroom => "wroom",
            Self::CoreS3 => "cores3",
            Self::Pico2W => "pico2w",
            Self::Linux => "linux",
            Self::Macos => "macos",
            Self::Windows => "windows",
            Self::Web => "web",
        }
    }
    pub fn default_profile(self) -> &'static str {
        match self {
            Self::Wroom => "esp32s3-camera-stream-startup",
            Self::CoreS3 => "m5stack-cores3-gnss",
            Self::Pico2W => "pico2w-radio",
            Self::Linux => "linux",
            Self::Macos => "macos",
            Self::Windows => "windows",
            Self::Web => "web",
        }
    }
    pub fn parse(name: &str) -> Result<Self> {
        Self::ALL.into_iter().find(|t|t.name()==name).ok_or_else(||format!("unknown platform {name}; choose wroom, cores3, pico2w, linux, macos, windows or web"))
    }
}
