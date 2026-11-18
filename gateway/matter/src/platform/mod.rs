//! Host file admission and the native interface binding std sockets cannot express.
cfg_if::cfg_if! {
    if #[cfg(any(target_os = "linux", target_os = "macos"))] {
        mod files;
        pub mod lan;
        pub(crate) use files::{create_directory, open_directory, open_file, same_file};
    } else {
        mod unavailable;
        pub(crate) use unavailable::{create_directory, open_directory, open_file, same_file};
    }
}
