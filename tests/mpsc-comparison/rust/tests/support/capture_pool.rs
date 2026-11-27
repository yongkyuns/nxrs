//! Thin fixture entry point to the production scalar transport.
//! The Thread notifier and test hooks are qualification choices; payload storage,
//! admission, publication and retirement are the same code used by the app.
#![forbid(unsafe_code)]

use messaging::pooled;
use std::thread::Thread;

pub use pooled::{
    CAPACITY, Captured, Key, OpenStage, Record, Rejected, Rejection, SOURCES, Stage, StartError,
    Status, StorageReport,
};
pub type Owner = pooled::Owner<Thread>;
pub type Publisher = pooled::Publisher<Thread>;

pub fn channel(owner: Thread) -> (Owner, [Publisher; SOURCES]) {
    pooled::channel(
        pooled::RetainedBudget {
            max_requested_bytes: 22 * 1024,
        },
        owner,
    )
    .expect("qualified retained pool fits its explicit setup budget")
}

pub fn storage_report() -> StorageReport {
    pooled::storage_report::<Thread>()
}
