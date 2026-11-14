use crate::{Display, Error, Rect, Size};

pub fn open() -> Result<impl Display, Error> {
    struct Unavailable;

    impl Display for Unavailable {
        fn size(&self) -> Size {
            Size {
                width: 0,
                height: 0,
            }
        }

        fn draw(&mut self, _: Rect, _: &[u16]) -> Result<(), Error> {
            Err(Error::Unsupported)
        }
    }

    Err::<Unavailable, _>(Error::Unsupported)
}
