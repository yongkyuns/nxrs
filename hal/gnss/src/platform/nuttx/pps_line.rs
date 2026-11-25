cfg_if::cfg_if! {
    if #[cfg(all(feature = "pps", not(test)))] {
        pub(super) type PpsLine = gpio::Interrupt<&'static core::sync::atomic::AtomicU8>;
    } else {
        use core::sync::atomic::AtomicU8;
        use std::io;

        pub(super) enum PpsLine {}

        impl PpsLine {
            pub(super) fn open_rising_with_context(
                _: &str,
                _: &'static AtomicU8,
                _: fn(&&'static AtomicU8),
            ) -> io::Result<Self> {
                Err(io::ErrorKind::Unsupported.into())
            }

            pub(super) fn unregister(&mut self) -> io::Result<()> {
                match *self {}
            }

            pub(super) fn take_timestamp(&self) -> io::Result<Option<(usize, u64)>> {
                match *self {}
            }
        }
    }
}
