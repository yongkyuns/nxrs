//! Select instrumentation once; acquisition uses the same private interface.
#[cfg_attr(feature = "probe", path = "trace/enabled.rs")]
#[cfg_attr(not(feature = "probe"), path = "trace/disabled.rs")]
mod selected;
pub(super) use selected::Trace;
