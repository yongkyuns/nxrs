//! Bind one authorized HTTP stream to the board's microphone and camera clock.
use stream_service::wire::{AudioBlock, AudioStamp, BLOCK_BYTES, BLOCK_SAMPLES, SAMPLE_RATE_HZ};
use std::time::{Duration, Instant};

const _: () = assert!(
    audio::SAMPLES_PER_BLOCK as u64 == BLOCK_SAMPLES && audio::SAMPLE_RATE_HZ == SAMPLE_RATE_HZ
);
const BLOCK_DURATION: Duration =
    Duration::from_millis(BLOCK_SAMPLES * 1000 / SAMPLE_RATE_HZ as u64);
const START_TIMEOUT: Duration = Duration::from_millis(250);

struct Pending {
    stamp: AudioStamp,
    pcm: [u8; BLOCK_BYTES],
}

impl Pending {
    fn from_capture(block: audio::Block, origin: Instant) -> Result<Self, ::tls::Error> {
        // Estimate the beginning of this block from the HAL's completion
        // observation. Queue delay remains part of the physical timing budget.
        let elapsed = block
            .frame
            .observed_at
            .checked_duration_since(origin)
            .ok_or(::tls::Error::Time)?;
        let capture_ms = elapsed
            .checked_sub(BLOCK_DURATION)
            .ok_or(::tls::Error::Time)?
            .as_millis()
            .try_into()
            .map_err(|_| ::tls::Error::Time)?;
        let mut pcm = [0; BLOCK_BYTES];
        for (out, sample) in pcm.as_chunks_mut::<2>().0.iter_mut().zip(block.samples) {
            out.copy_from_slice(&sample.to_le_bytes());
        }
        Ok(Self {
            stamp: AudioStamp {
                first_sample: block.frame.first_sample,
                capture_ms,
            },
            pcm,
        })
    }
}

pub(crate) struct Microphone {
    origin: Instant,
    stream: Box<dyn audio::Stream>,
    startup: Option<Instant>,
    pending: Option<Pending>,
}

impl Microphone {
    pub(crate) fn start(origin: Instant) -> Result<Self, ::tls::Error> {
        let stream = audio::stream(16 * 1024).map_err(|_| ::tls::Error::Io)?;
        Ok(Self {
            origin,
            stream: Box::new(stream),
            startup: Some(Instant::now() + START_TIMEOUT),
            pending: None,
        })
    }

    /// Do not emit a positive A/V response until a complete block arrives.
    pub(crate) fn ready(&mut self) -> Result<bool, ::tls::Error> {
        if self.startup.is_some_and(|until| Instant::now() >= until) {
            return Err(::tls::Error::Io);
        }
        if let Some(block) = self.stream.take().map_err(|_| ::tls::Error::Io)? {
            self.pending = Some(Pending::from_capture(block, self.origin)?);
            self.startup = None;
        }
        Ok(self.startup.is_none())
    }

    pub(crate) fn block(&self) -> Option<AudioBlock<'_>> {
        self.pending.as_ref().map(|pending| AudioBlock {
            stamp: pending.stamp,
            pcm: &pending.pcm,
        })
    }

    pub(crate) fn accepted(&mut self) {
        self.pending = None;
    }

    pub(crate) fn stop(&mut self) -> Result<(), ::tls::Error> {
        self.pending = None;
        self.stream.stop().map_err(|_| ::tls::Error::Io)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;

    struct Source(VecDeque<Option<audio::Block>>);
    impl audio::Stream for Source {
        fn take(&mut self) -> Result<Option<audio::Block>, audio::DeviceError> {
            Ok(self.0.pop_front().flatten())
        }
        fn stop(&mut self) -> Result<(), audio::DeviceError> {
            Ok(())
        }
    }

    #[test]
    fn waits_for_capture_then_uses_the_camera_clock_and_sample_identity() {
        let origin = Instant::now();
        let source = Source(VecDeque::from([
            None,
            Some(audio::Block {
                frame: audio::Frame {
                    first_sample: BLOCK_SAMPLES * 2,
                    observed_at: origin + Duration::from_millis(67),
                },
                samples: [-321; audio::SAMPLES_PER_BLOCK],
            }),
        ]));
        let mut microphone = Microphone {
            origin,
            stream: Box::new(source),
            startup: Some(origin + START_TIMEOUT),
            pending: None,
        };
        assert!(!microphone.ready().unwrap());
        assert!(microphone.block().is_none());
        assert!(microphone.ready().unwrap());
        let block = microphone.block().unwrap();
        assert_eq!(
            block.stamp,
            AudioStamp {
                first_sample: BLOCK_SAMPLES * 2,
                capture_ms: 47
            }
        );
        assert_eq!(block.pcm[..2], (-321i16).to_le_bytes());
        microphone.accepted();
        assert!(microphone.ready().unwrap());
        assert!(microphone.block().is_none());
        microphone.startup = Some(Instant::now());
        assert_eq!(microphone.ready(), Err(::tls::Error::Io));
    }
}
