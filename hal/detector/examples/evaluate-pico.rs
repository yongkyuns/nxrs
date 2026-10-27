use detector::{Detector, Image};
use std::{
    io::{BufRead, Write},
    time::Instant,
};
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let path = std::env::args_os().nth(1).ok_or("expected manifest path")?;
    let input = std::io::BufReader::new(std::fs::File::open(path)?);
    let mut boxes = std::env::args_os()
        .nth(2)
        .map(std::fs::File::create)
        .transpose()?;
    let mut detector =
        detector::open(detector::Config::default()).map_err(|e| format!("open: {e:?}"))?;
    let mut output = std::io::BufWriter::new(std::io::stdout().lock());
    for (i, line) in input.lines().enumerate() {
        let line = line?;
        let fields: Vec<_> = line.split('\t').collect();
        if fields.len() != 4 {
            return Err("invalid manifest".into());
        }
        let width = fields[1].parse()?;
        let height = fields[2].parse()?;
        let jpeg = std::fs::read(fields[3])?;
        let start = Instant::now();
        let result = detector
            .detect(Image::jpeg(&jpeg, width, height))
            .map_err(|e| format!("image {i}: {e:?}"))?;
        let elapsed_us = start.elapsed().as_micros();
        if let Some(file) = boxes.as_mut() {
            write!(file, "{i}\t{}", u8::from(result.objects.truncated))?;
            for item in result.objects.iter() {
                let b = item.bounds;
                write!(
                    file,
                    "\t{},{},{},{},{}",
                    item.score.permille(),
                    b.left,
                    b.top,
                    b.right,
                    b.bottom
                )?;
            }
            writeln!(file)?;
        }
        // Fifth column is unavailable: the HAL intentionally keeps preprocessing
        // private. The paired report uses only the full detect call's elapsed time.
        writeln!(
            output,
            "{i}\t{}\t{}\t{}\t0",
            fields[0],
            result.person_score.permille(),
            elapsed_us
        )?;
    }
    Ok(())
}
