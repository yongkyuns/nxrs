//! Native stack witness; production application remains platform independent.
#[allow(dead_code)]
#[path = "../../../../../app/ao-stress/src/main.rs"]
mod application;
#[allow(dead_code)]
mod stack_observer;
fn main() {
    #[cfg(target_os = "nuttx")]
    let result = application::run(Some(stack_observer::report_stacks));
    #[cfg(not(target_os = "nuttx"))]
    let result: Result<(), String> = Err("requires native stack qualification".into());
    if let Err(error) = result {
        eprintln!("AO_STRESS FAIL: {error}");
        std::process::exit(1);
    }
}
