//! Optional instrumentation of the actual capture publication boundary.
//! Test orchestration belongs to the consumer; normal record calls are unchanged.
use super::{CapturePublisher, Sample};
pub use messaging::pooled::{Rejected, Stage};
use navigation::observations::Measurement;

/// Publish with hooks inside the reserved capture pool.
///
/// # Panics
/// Panics if export storage was not reserved or the measurement uses a different
/// source. This instruments scalar pool publication; it does not update UTC state.
pub fn record_with_hooks(
    publisher: &mut CapturePublisher,
    measurement: Measurement,
    hook: impl FnMut(Stage),
) -> Result<(), Rejected> {
    let source = match measurement {
        Measurement::Imu(index, _) => {
            assert!(index < 2);
            index
        }
        Measurement::Gnss(_) => 2,
    };
    assert_eq!(
        source,
        usize::from(publisher.source),
        "capture source mismatch"
    );
    let publisher = publisher
        .publisher
        .as_mut()
        .expect("capture probe requires reserved storage");
    let epoch = publisher.captured_epoch();
    publisher.offer_with_hooks(epoch, Sample::from(measurement).record(), hook)
}
