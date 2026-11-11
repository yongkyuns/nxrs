//! Application build controller. HAL build scripts depend on `nxrs-build-support`.
#![forbid(unsafe_code)]
pub use nxrs_build_support::{
    archive, fetch_bounded, fetch_verified, find_program, find_program_on_path, hash, hash_bytes,
    json, native, output, output_bytes, profile, read, read_json, regex, remove_dir, remove_file,
    run, shell_quote, split_flags, write_json, Result, TempDir,
};
pub mod boot;
pub mod build;
pub mod cargo_graph;
pub mod commands;
pub mod deployment;
pub use nxrs_detector_build_support as detector_sdk;
pub mod factory;
pub mod inspect;
pub mod linker;
pub mod nuttx;
pub mod radio;
pub mod reuse;
pub mod sdk;
pub mod source;
pub mod web;
