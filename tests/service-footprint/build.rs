#[path = "../event-services-comparison/experiment.rs"]
#[allow(dead_code)]
mod event_experiment;
#[path = "src/experiment.rs"]
#[allow(dead_code)]
mod experiment;
fn main() {
    event_experiment::configure();
    println!("cargo:rerun-if-env-changed=NXRS_FOOTPRINT_CASE");
    println!("cargo:rerun-if-changed=src/experiment.rs");
    let name = std::env::var("NXRS_FOOTPRINT_CASE").unwrap_or_else(|_| "payload".into());
    let case = experiment::Experiment::parse(&name).unwrap_or_else(|error| panic!("{error}"));
    let source = format!("pub const CASE: Experiment = Experiment {{ workload: Workload::{:?}, io: Io::{:?}, paired: {} }};\npub const FILTER_WORDS: usize = if CASE.workload.packet() {{ 3 }} else {{ 0 }};\n#[cfg(target_os = \"nuttx\")] pub use crate::mq_io::{:?} as SelectedIo;\n", case.workload, case.io, case.paired, case.io);
    std::fs::write(
        std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap()).join("footprint_case.rs"),
        source,
    )
    .unwrap();
}
