use crate::{Error, Event, Session};

pub fn open(_: impl FnMut(Event) -> bool + Send + 'static) -> Result<impl Session, Error> {
    struct Unavailable;
    impl Session for Unavailable {
        fn is_finished(&self) -> bool {
            true
        }
        fn request_stop(&mut self) -> Result<(), Error> {
            Ok(())
        }
        fn join(&mut self) -> Result<(), Error> {
            Ok(())
        }
    }
    Err::<Unavailable, _>(Error::Unsupported)
}
