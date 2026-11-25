use crate::{Error, Geometry, Partition};

pub fn open() -> Result<impl Partition, Error> {
    struct Unavailable;

    impl Partition for Unavailable {
        fn geometry(&self) -> Geometry {
            unreachable!()
        }

        fn read(&mut self, _: u32, _: &mut [u8]) -> Result<(), Error> {
            Err(Error::Unsupported)
        }

        fn program(&mut self, _: u32, _: &[u8]) -> Result<(), Error> {
            Err(Error::Unsupported)
        }

        fn erase(&mut self, _: u32, _: u32) -> Result<(), Error> {
            Err(Error::Unsupported)
        }
    }

    Err::<Unavailable, _>(Error::Unsupported)
}
