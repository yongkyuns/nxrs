pub fn open() -> Result<impl crate::Store, crate::Error> {
    struct Unavailable;
    impl crate::Store for Unavailable {
        fn get(&mut self, _: &str, _: &mut [u8]) -> Result<Option<usize>, crate::Error> {
            Err(crate::Error::Unsupported)
        }
        fn set(&mut self, _: &str, _: &[u8]) -> Result<(), crate::Error> {
            Err(crate::Error::Unsupported)
        }
        fn remove(&mut self, _: &str) -> Result<(), crate::Error> {
            Err(crate::Error::Unsupported)
        }
    }
    Err::<Unavailable, _>(crate::Error::Unsupported)
}
