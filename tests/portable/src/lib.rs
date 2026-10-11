//! Mock devices and a cross-platform executable qualification suite.
#![deny(unsafe_op_in_unsafe_fn)]

pub mod mocks {
    pub use camera::{CameraAction, MockCamera, FORMAT};
    pub use storage::{MockStorage, StoredFrame};
    pub use transport::MockTransport;
}
pub mod scenarios;

#[cfg(not(target_arch = "wasm32"))]
pub mod threaded;

// Only the simulator exports this ABI. Production application crates remain
// no_std and forbid unsafe code. Assertions are active in release WASM too.
#[cfg(target_arch = "wasm32")]
mod wasm {
    use super::scenarios::SCENARIOS;

    #[no_mangle]
    pub extern "C" fn scenario_count() -> u32 {
        SCENARIOS.len() as u32
    }

    #[no_mangle]
    pub extern "C" fn scenario_name_ptr(index: u32) -> *const u8 {
        SCENARIOS.get(index as usize).map_or(core::ptr::null(), |s| s.name.as_ptr())
    }

    #[no_mangle]
    pub extern "C" fn scenario_name_len(index: u32) -> u32 {
        SCENARIOS.get(index as usize).map_or(0, |s| s.name.len() as u32)
    }

    #[no_mangle]
    pub extern "C" fn scenario_run(index: u32) -> u32 {
        let Some(scenario) = SCENARIOS.get(index as usize) else {
            return 0;
        };
        (scenario.run)();
        1
    }
}
