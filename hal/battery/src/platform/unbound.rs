pub mod devices {
    use crate::{Device, Error, Status};

    struct Unavailable;

    impl Device for Unavailable {
        fn status(&self) -> Result<Status, Error> {
            Err(Error::Unsupported)
        }
    }

    pub fn primary_battery() -> &'static impl Device {
        &Unavailable
    }
}
