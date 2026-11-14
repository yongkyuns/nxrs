#[cfg(target_os = "nuttx")]
mod lvgl;

#[cfg(all(target_os = "nuttx", feature = "framebuffer"))]
mod framebuffer;

#[cfg(all(target_os = "nuttx", feature = "framebuffer"))]
fn main() {
    framebuffer::qualify();
}

#[cfg(all(target_os = "nuttx", not(feature = "framebuffer")))]
fn main() {
    use display::Display;

    let mut screen = display::open().expect("open display");
    let size = screen.size();
    let chunks = match lvgl::render_label(&mut screen, c"CoreS3 display ready") {
        Ok(chunks) => chunks,
        Err(lvgl::Error::Display(error)) => panic!("write display: {error:?}"),
        Err(error) => panic!("render screen: {error:?}"),
    };
    println!(
        "LVGL PASS {}x{} RGB565 ({chunks} flushes)",
        size.width, size.height
    );
}

#[cfg(not(target_os = "nuttx"))]
fn main() {
    eprintln!("Build this qualification with NuttX LVGL enabled.");
    std::process::exit(1);
}
