//! Task-context accounting witnesses using the production peripheral module.
//! No native ABI, controller timing or radio delivery is emulated.
use super::*;

static SERIAL: Mutex<()> = Mutex::new(());

pub(super) mod ffi {
    #![allow(non_upper_case_globals)]
    pub static peripheral_max_connections: u32 = 1;
    pub unsafe fn peripheral_register(_: *const u8, _: bool) {}
    pub unsafe fn peripheral_start() -> i32 {
        0
    }
    pub unsafe fn peripheral_stop() {}
    pub struct Peer;
    impl Peer {
        pub fn lookup(_: u16) -> Option<Self> {
            Some(Self)
        }
        pub fn notify(&self, bytes: &[u8]) -> std::io::Result<usize> {
            Ok(bytes.len())
        }
        pub fn disconnect(&self) {
            super::super::peripheral_connection_rust(false, 0);
        }
    }
}

fn connect(notify: bool) -> (impl Listener, impl Link) {
    let service = Service {
        uuid: Uuid([1; 16]),
        ingress: Uuid([2; 16]),
        egress: Uuid([3; 16]),
    };
    let notifications = Service {
        uuid: Uuid([4; 16]),
        ingress: Uuid([5; 16]),
        egress: Uuid([6; 16]),
    };
    let mut listener = open_peripheral(service, Some(notifications)).unwrap();
    peripheral_connection_rust(true, 1);
    peripheral_notify_rust(notify);
    let transport = listener.accept().unwrap().unwrap();
    (listener, transport)
}

fn progress(listener: &impl Listener) -> DeliveryProgress {
    listener.delivery_progress().unwrap().unwrap()
}

#[test]
fn notification_progress_requires_a_valid_ack_even_with_output_still_pending() {
    let _serial = SERIAL.lock().unwrap();
    let (mut listener, mut transport) = connect(true);
    let initial = progress(&listener);
    assert!(!initial.pending);
    transport.write_all(b"abcdef").unwrap();
    listener.service().unwrap();
    assert_eq!(
        progress(&listener),
        DeliveryProgress {
            pending: true,
            ..initial
        }
    );
    assert_eq!(
        transport.flush().unwrap_err().kind(),
        io::ErrorKind::WouldBlock
    );
    output(initial.generation).unwrap().replay_at = Some(Instant::now());
    listener.service().unwrap();
    assert_eq!(
        progress(&listener),
        DeliveryProgress {
            pending: true,
            ..initial
        }
    );

    let ack = [1, 2, 0, 0, 0];
    assert_eq!(unsafe { peripheral_write_rust(ack.as_ptr(), 5, true) }, 5);
    listener.service().unwrap();
    let partial = progress(&listener);
    assert_eq!(partial.completed, 2);
    assert!(partial.pending);
    assert_eq!(
        transport.flush().unwrap_err().kind(),
        io::ErrorKind::WouldBlock
    );

    // Duplicate and old acknowledgments do not claim more progress.
    for offset in [2_u32, 1] {
        let mut ack = [1, 0, 0, 0, 0];
        ack[1..].copy_from_slice(&offset.to_le_bytes());
        unsafe {
            peripheral_write_rust(ack.as_ptr(), 5, true);
        }
        listener.service().unwrap();
        assert_eq!(progress(&listener), partial);
    }
    let invalid = [1, 7, 0, 0, 0];
    unsafe {
        peripheral_write_rust(invalid.as_ptr(), 5, true);
    }
    assert_eq!(
        listener.service().unwrap_err().kind(),
        io::ErrorKind::InvalidData
    );
    assert_eq!(progress(&listener), partial);
}

#[test]
fn legacy_reads_advance_transport_consumption_without_claiming_notification_acks() {
    let _serial = SERIAL.lock().unwrap();
    let (listener, mut transport) = connect(false);
    transport.write_all(b"abcd").unwrap();
    let mut bytes = [0; 2];
    assert_eq!(unsafe { peripheral_read_rust(bytes.as_mut_ptr(), 2) }, 2);
    assert_eq!(&bytes, b"ab");
    let partial = progress(&listener);
    assert_eq!((partial.completed, partial.pending), (2, true));
    assert_eq!(ACKNOWLEDGED.load(Ordering::Acquire), 0);
    assert_eq!(unsafe { peripheral_read_rust(bytes.as_mut_ptr(), 2) }, 2);
    assert_eq!(&bytes, b"cd");
    assert_eq!(
        (progress(&listener).completed, progress(&listener).pending),
        (4, false)
    );
    transport.flush().unwrap();
}

#[test]
fn a_replacement_connection_has_its_own_delivery_generation_and_empty_accounting() {
    let _serial = SERIAL.lock().unwrap();
    let (mut listener, mut first) = connect(false);
    first.write_all(b"ab").unwrap();
    let mut bytes = [0; 1];
    unsafe {
        peripheral_read_rust(bytes.as_mut_ptr(), 1);
    }
    let old = progress(&listener);
    drop(first);
    assert_eq!(
        listener.delivery_progress().unwrap_err().kind(),
        io::ErrorKind::NotConnected
    );
    peripheral_connection_rust(false, 1);
    peripheral_connection_rust(true, 1);
    let _replacement = listener.accept().unwrap().unwrap();
    let current = progress(&listener);
    assert_ne!(current.generation, old.generation);
    assert_eq!((current.completed, current.pending), (0, false));
}
