//! Concrete UART receiver selection from validated product bindings.
use ::gnss::{
    construction::{Instance, Serial},
    Device,
};
macro_rules! declare_device {
    ($name:ident, $id:literal, $receiver:ty, $path:literal, $baud:literal, $stack:literal, $reference:tt, $period:literal $(, pps = $pps:literal)?) => {
        static $name: Instance<$receiver> = Instance::new($id, $path, $baud, $stack, |serial| {
            <$receiver>::new(serial).with_uart_baud($baud).with_navigation_period($period)
        })$(.with_pps($pps))?;
    };
    ($name:ident, $id:literal, $receiver:ty, $path:literal, $baud:literal, $stack:literal, $reference:tt $(, pps = $pps:literal)?) => {
        static $name: Instance<$receiver> = Instance::new($id, $path, $baud, $stack, |serial| {
            <$receiver>::new(serial).with_uart_baud($baud)
        })$(.with_pps($pps))?;
    };
}
include!(concat!(env!("OUT_DIR"), "/gnss_config.rs"));
