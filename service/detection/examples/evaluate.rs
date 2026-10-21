//! Manifest lines: LABEL<TAB>WIDTH<TAB>HEIGHT<TAB>ABSOLUTE_JPEG_PATH.
//! Output: index, label, score permille, preprocessing+inference microseconds,
//! preprocessing microseconds. File I/O is outside the measured interval.
use detection_service::person::MobileNet;
use std::{
    io::{BufRead, Write},
    time::Instant,
};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let manifest = std::env::args_os().nth(1).ok_or("expected manifest path")?;
    let input = std::io::BufReader::new(std::fs::File::open(manifest)?);
    let pixels_directory = std::env::args_os().nth(2).map(std::path::PathBuf::from);
    if let Some(path) = pixels_directory.as_ref() {
        std::fs::create_dir(path)?;
    }
    let mut output = std::io::BufWriter::new(std::io::stdout().lock());
    let mut detector = MobileNet::default();
    for (index, line) in input.lines().enumerate() {
        let line = line?;
        let parts: Vec<_> = line.split('\t').collect();
        if parts.len() != 4 {
            return Err("invalid manifest line".into());
        }
        let (width, height): (u16, u16) = (parts[1].parse()?, parts[2].parse()?);
        let bytes = std::fs::read(parts[3])?;
        let start = Instant::now();
        let pixels = detector
            .preprocess(&bytes, width, height)
            .map_err(|e| format!("image {index}: {e:?}"))?;
        let preprocess_us = start.elapsed().as_micros();
        let score = detector
            .rgb(&pixels)
            .map_err(|e| format!("image {index}: {e:?}"))?;
        let total_us = start.elapsed().as_micros();
        if let Some(path) = pixels_directory.as_ref() {
            std::fs::write(path.join(format!("{index}.rgb")), pixels)?;
        }
        writeln!(
            output,
            "{index}\t{}\t{}\t{total_us}\t{preprocess_us}",
            parts[0],
            score.permille()
        )?;
    }
    Ok(())
}
