use crate::{Error, QuiescenceControl};

struct Unavailable;

impl QuiescenceControl for Unavailable {
    fn quiesce(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    fn resume(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
}

pub(crate) fn open_quiescence() -> Result<impl QuiescenceControl, Error> {
    Err::<Unavailable, _>(Error::Unsupported)
}
