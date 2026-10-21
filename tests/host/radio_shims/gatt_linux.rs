use super::*;
fn code(e: Error) -> i32 {
    err(match e {
        Error::InvalidValue => 4,
        Error::Busy => 5,
        Error::Io => 6,
        Error::Unavailable => 7,
        Error::Unsupported => 12,
        Error::PermissionDenied => 13,
        Error::Timeout => 18,
    })
}
fn disconnected() -> i32 {
    err(7)
}
include!("gatt.rs");
#[no_mangle]
unsafe extern "C" fn nxrs_linux_gatt_test_contend(session: u64) {
    let _guard = FRONT.lock().unwrap();
    assert!(Executor::poll(SessionId::new(session).unwrap()).is_none());
}
#[no_mangle]
extern "C" fn nxrs_linux_gatt_test_saturate() {
    FRONT.lock().unwrap().as_mut().unwrap().events = VecDeque::from([BackendEvent::Fault; 4]);
}
