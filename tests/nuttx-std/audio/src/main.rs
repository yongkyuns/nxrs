//! Explicit capture and reopen witness. Never print or persist household audio.
use audio::{Capture, Frame, SAMPLES_PER_BLOCK};
use std::time::{Duration, Instant};

fn main() {
    for cycle in 0..2 {
        let mut capture = audio::open().expect("open microphone capture");
        let mut samples = [0; SAMPLES_PER_BLOCK];
        for block in 0..4 {
            let Frame { first_sample, .. } = capture
                .read(Instant::now() + Duration::from_secs(1), &mut samples)
                .expect("read a complete microphone block");
            assert_eq!(first_sample, block * SAMPLES_PER_BLOCK as u64);
        }
        capture
            .stop(Instant::now() + Duration::from_secs(1))
            .expect("retire the microphone and every buffer");
        println!(
            "audio-capture: session {} returned four full blocks and stopped",
            cycle + 1
        );
    }
}
