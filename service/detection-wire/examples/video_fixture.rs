//! Reproduce the shared SEI fixture; contains metadata only, no encoded image.
use detection_wire::{Snapshot, Version, VideoFrame};
fn main() {
    let source: Snapshot = serde_json::from_slice(include_bytes!("../../../clients/ios/Tests/NxrsMonitorCoreTests/Fixtures/detection.json")).unwrap();
    let frame = VideoFrame { api_version: Version::V1, epoch: "a".repeat(32), frame_sequence: 20, frame_capture_ms: 1100, source: source.source };
    let directory = std::path::PathBuf::from(std::env::args_os().nth(1).expect("fixture output directory"));
    std::fs::write(directory.join("video-detection.json"), serde_json::to_vec_pretty(&frame).unwrap()).unwrap();
    std::fs::write(directory.join("video-detection-sei.bin"), frame.sei().unwrap()).unwrap();
}
