use super::*;
fn code(e: Error) -> i32 {
    err(match e {
        Error::InvalidValue => 1,
        Error::Busy => 2,
        Error::Io => 3,
        Error::Unavailable => 4,
        Error::Unsupported => 9,
        Error::PermissionDenied => 10,
        Error::Timeout => 3,
    })
}
fn disconnected() -> i32 {
    err(4)
}
include!("gatt.rs");
#[no_mangle]
extern "C" fn nxrs_mac_gatt_test_session() -> u64 {
    STATE.lock().unwrap().as_ref().map_or(0, |s| s.session)
}
#[no_mangle]
unsafe extern "C" fn nxrs_mac_gatt_test_contend(inject: bool) {
    let mut guard = STATE.lock().unwrap();
    let s = guard.as_mut().unwrap();
    let session = SessionId::new(s.session).unwrap();
    if inject {
        s.events.push_back(BackendEvent::Completed {
            operation: 1,
            result: Ok(Reply::Connected),
        });
    }
    let c = Operation {
        operation: 1,
        command: Command::Connect {
            peer: Peer {
                session,
                id: PeerId(1),
            },
            identity: Some([0; 16]),
        },
    };
    assert_eq!(Executor::submit(session, c), Err(Error::Busy));
    assert!(Executor::poll(session).is_none());
}
#[no_mangle]
extern "C" fn nxrs_mac_gatt_test_saturate() {
    STATE.lock().unwrap().as_mut().unwrap().events = VecDeque::from([BackendEvent::Fault; 4]);
}
