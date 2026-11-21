use crate::{Error, Event, Session, Stats, Waveform};

pub fn open<F: FnMut(Event) -> bool + Send + 'static>(_: F) -> Result<impl Session, Error> {
    struct Unavailable;
    impl Session for Unavailable {
        fn configure(&mut self, _: Waveform) -> Result<(), Error> {
            Err(Error::Unsupported)
        }
        fn disable(&mut self) -> Result<(), Error> {
            Err(Error::Unsupported)
        }
        fn stats(&self) -> Stats {
            Stats::default()
        }
        fn shutdown(&mut self) -> Result<Stats, Error> {
            Err(Error::Unsupported)
        }
    }
    Err::<Unavailable, _>(Error::Unsupported)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unbound_factory_never_calls_the_sink() {
        assert!(matches!(
            open(|_| panic!("unbound capability must not emit a fabricated event")),
            Err(Error::Unsupported)
        ));
    }
}
