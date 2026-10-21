//! Exercises just the selected provider, with no app/storage/network dependency.
#[cfg(nxrs_hal_platform = "std")]
use camera::{Camera, Format, PixelFormat};
#[cfg(nxrs_hal_platform = "std")]
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let path = std::env::args().nth(1).ok_or("expected raw file path")?;
    let format = Format {
        width: 2,
        height: 2,
        pixels: PixelFormat::Gray8,
    };
    let (mut camera, count) = camera::replay(&path, format, 10, 8)?;
    assert_eq!(count, 2);
    assert_eq!(camera.start(format), Ok(format));
    let mut bytes = [0; 4];
    assert_eq!(
        camera
            .poll_frame(100, &mut bytes)
            .unwrap()
            .unwrap()
            .timestamp_ms,
        100
    );
    assert_eq!(bytes, [7; 4]);
    assert_eq!(camera.poll_frame(109, &mut bytes), Ok(None));
    assert_eq!(
        camera
            .poll_frame(110, &mut bytes)
            .unwrap()
            .unwrap()
            .timestamp_ms,
        110
    );
    assert_eq!(bytes, [9; 4]);
    assert_eq!(camera.stop(), Ok(()));
    println!("CAMERA_ONLY_PASS");
    Ok(())
}

#[cfg(not(nxrs_hal_platform = "std"))]
fn main() {
    eprintln!("This example requires NXRS_PLATFORM=std");
    std::process::exit(2);
}
