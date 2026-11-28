use crate::{Capabilities, Control, Error};
use std::time::SystemTime;

pub fn open() -> Result<impl Control, Error> {
    struct Unavailable;

    impl Control for Unavailable {
        fn capabilities(&self) -> Capabilities {
            Capabilities {
                slew_rate_ppm: None,
            }
        }

        fn set(&mut self, _: SystemTime) -> Result<(), Error> {
            Err(Error::Unsupported)
        }

        fn replace_adjustment(&mut self, _: i64) -> Result<i64, Error> {
            Err(Error::Unsupported)
        }

        fn pending_adjustment(&mut self) -> Result<i64, Error> {
            Err(Error::Unsupported)
        }
    }

    Err::<Unavailable, _>(Error::Unsupported)
}
