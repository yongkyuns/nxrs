#[cfg(any(all(hal_enabled, target_os = "nuttx"), all(test, unix)))]
#[cfg_attr(test, allow(dead_code))]
mod nuttx;

#[cfg(all(hal_enabled, target_os = "nuttx"))]
pub fn open() -> Result<impl crate::Store, crate::Error> {
    nuttx::open()
}

#[cfg(not(hal_enabled))]
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
