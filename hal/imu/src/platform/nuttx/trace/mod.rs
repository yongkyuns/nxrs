//! Select instrumentation once; acquisition uses the same private interface.
#[cfg(not(feature = "probe"))]
mod disabled;
#[cfg(feature = "probe")]
mod enabled;

#[cfg(not(feature = "probe"))]
pub use disabled::{Observed, Trace};
#[cfg(feature = "probe")]
pub use enabled::{Observed, Trace};
