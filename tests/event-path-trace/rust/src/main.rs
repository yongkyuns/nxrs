#[cfg_attr(not(target_os = "nuttx"), allow(dead_code))]
mod buffer;
#[cfg(target_os = "nuttx")]
include!("firmware.rs");

#[cfg(not(target_os = "nuttx"))]
fn main() {
    eprintln!("P4 refuses host execution; unit tests do not qualify target traces");
    std::process::exit(2);
}
