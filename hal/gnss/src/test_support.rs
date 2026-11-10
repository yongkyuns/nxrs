//! Explicit synthetic source used by session ownership tests.
#[cfg(not(platform = "mock"))]
#[allow(dead_code, unused_imports)]
#[path = "platform/mock.rs"]
pub(crate) mod mock_tests;
#[cfg(platform = "mock")]
pub(crate) use crate::platform as mock_tests;
