//! Concrete UART receiver selection from validated product bindings.
use ::gnss::{
    construction::{Instance, Serial},
    Device,
};
macro_rules! declare_device {
    ($name:ident, $id:literal, $receiver:ty, $path:literal, $baud:literal, $stack:literal) => {
        static $name: Instance<$receiver> =
            Instance::new($id, $path, $baud, $stack, <$receiver>::new);
    };
}
include!(concat!(env!("OUT_DIR"), "/gnss_config.rs"));
