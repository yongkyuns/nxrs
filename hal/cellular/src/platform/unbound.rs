use crate::{Error, Session, Status};

/// Whether this build can establish a cellular link.
pub const LINK_AVAILABLE: bool = false;

pub fn connect(_: &str) -> Result<impl Session, Error> {
    struct Unavailable;
    impl Session for Unavailable {
        fn status(&self) -> Status {
            Status::Failed(Error::Unsupported)
        }
        fn request_stop(&mut self) {}
        fn is_finished(&self) -> bool {
            true
        }
        fn join(&mut self) -> Result<(), Error> {
            Ok(())
        }
    }
    Err::<Unavailable, _>(Error::Unsupported)
}

pub fn connect_managed(
    apn: &str,
    _: impl Fn(Status) + Send + Sync + 'static,
) -> Result<impl Session, Error> {
    connect(apn)
}
