//! Concrete UART receiver selection from validated product bindings.
use ::gnss::{
    construction::{Instance, Serial},
    Device,
};
#[cfg(feature = "ubx")]
mod implementations {
    pub(super) type NeoM9n = nxrs_ubx_driver::Driver<super::Serial>;
}
macro_rules! declare_device {
    ($name:ident, $id:literal, $receiver:ty, $path:literal, $baud:literal, $stack:literal) => {
        static $name: Instance<$receiver> =
            Instance::new($id, $path, $baud, $stack, <$receiver>::new);
    };
}
include!(concat!(env!("OUT_DIR"), "/gnss_config.rs"));
