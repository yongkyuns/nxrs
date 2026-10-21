//! CoreBluetooth UUID data uses network byte order.
pub(crate) fn canonical(bytes: &[u8]) -> Option<[u8; 16]> {
    let mut value = [
        0, 0, 0, 0, 0, 0, 0x10, 0, 0x80, 0, 0, 0x80, 0x5f, 0x9b, 0x34, 0xfb,
    ];
    match bytes.len() {
        2 => value[2..4].copy_from_slice(bytes),
        4 => value[..4].copy_from_slice(bytes),
        16 => value.copy_from_slice(bytes),
        _ => return None,
    }
    Some(value)
}
