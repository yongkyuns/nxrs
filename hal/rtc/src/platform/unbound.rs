use crate::{Clock, Error};
use std::time::SystemTime;

pub fn open() -> Result<impl Clock, Error> {
    struct Unavailable;

    impl Clock for Unavailable {
        fn read(&self) -> Result<Option<SystemTime>, Error> {
            Err(Error::Unsupported)
        }

        fn set(&mut self, _: SystemTime) -> Result<(), Error> {
            Err(Error::Unsupported)
        }
    }

    Err::<Unavailable, _>(Error::Unsupported)
}
