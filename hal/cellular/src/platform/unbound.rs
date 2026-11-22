use crate::{Error, Session, Status};

pub fn connect(_: &str) -> Result<impl Session, Error> {
    struct Unavailable;
    impl Session for Unavailable {
        fn status(&self) -> Status {
            Status::Failed(Error::Unsupported)
        }
        fn stop(&mut self) -> Result<(), Error> {
            Ok(())
        }
    }
    Err::<Unavailable, _>(Error::Unsupported)
}
