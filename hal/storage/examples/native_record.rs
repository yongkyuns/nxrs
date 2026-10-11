//! Storage-only fixture: std files and worker, no camera/transport provider.
#[cfg(nxrs_provider = "native")]
use storage::{Capture, FileRecorder, Format, Frame, PixelFormat, Storage, StorageLimits};

#[cfg(nxrs_provider = "native")]
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let directory = std::env::args()
        .nth(1)
        .ok_or("expected a new output directory")?;
    let mut storage = FileRecorder::create_new(
        &directory,
        StorageLimits {
            queued_records: 2,
            max_frame_bytes: 4,
            max_records: 2,
        },
    )?;
    let format = Format {
        width: 2,
        height: 2,
        pixels: PixelFormat::Gray8,
    };
    for (sequence, bytes) in [(1, [7; 4]), (2, [9; 4])] {
        storage
            .append(Frame {
                sequence,
                capture: Capture {
                    format,
                    len: bytes.len(),
                    timestamp_ms: sequence * 10,
                },
                bytes: &bytes,
            })
            .map_err(|e| format!("append: {e:?}"))?;
    }
    storage.finish().map_err(|e| format!("commit: {e:?}"))?;
    println!("STORAGE_ONLY_PASS");
    Ok(())
}

#[cfg(not(nxrs_provider = "native"))]
fn main() {
    eprintln!("This example requires NXRS_PLATFORM=native");
    std::process::exit(2);
}
