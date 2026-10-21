#![allow(dead_code)]
pub(crate) const CAPACITY: usize = 64;

#[repr(C)]
#[derive(Clone, Copy)]
pub(crate) struct RawCommand {
    pub operation: u64,
    pub peer_session: u64,
    pub peer_id: u64,
    pub kind: u32,
    pub handle: u16,
    pub length: u16,
    pub service: [u8; 16],
    pub characteristic: [u8; 16],
    pub data: [u8; CAPACITY],
}

#[repr(C)]
#[derive(Clone, Copy)]
pub(crate) struct RawEvent {
    pub operation: u64,
    pub kind: u32,
    pub error: i32,
    pub handle: u16,
    pub length: u16,
    pub att_error: u8,
    pub reason: u8,
    pub data: [u8; CAPACITY],
}

impl RawCommand {
    pub const EMPTY: Self = Self {
        operation: 0,
        peer_session: 0,
        peer_id: 0,
        kind: 0,
        handle: 0,
        length: 0,
        service: [0; 16],
        characteristic: [0; 16],
        data: [0; CAPACITY],
    };
}
impl RawEvent {
    pub const EMPTY: Self = Self {
        operation: 0,
        kind: 0,
        error: 0,
        handle: 0,
        length: 0,
        att_error: 0,
        reason: 0,
        data: [0; CAPACITY],
    };
}
