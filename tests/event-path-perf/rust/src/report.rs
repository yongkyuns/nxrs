use crate::{native::Schedule, support::Config};

#[derive(Debug)]
pub struct Sample {
    pub sequence: u32,
    pub provider: u32,
    pub start: u32,
    pub admission_end: u32,
    pub receive_start: Option<u32>,
    pub receive: u32,
    pub window_ns: u64,
    pub blocked: bool,
    pub peer_tid: i32,
    pub park_returns: u32,
    pub accepted: bool,
}

impl Default for Sample {
    fn default() -> Self {
        Self {
            sequence: 0,
            provider: 0,
            start: 0,
            admission_end: 0,
            receive_start: None,
            receive: 0,
            window_ns: 0,
            blocked: false,
            peer_tid: 0,
            park_returns: 0,
            accepted: true,
        }
    }
}

#[derive(Default)]
pub struct Counts {
    pub attempted: u32,
    pub admitted: u32,
    pub rejected: u32,
    pub handled: u32,
}

pub struct Report {
    pub samples: Vec<Sample>,
    pub sender: Schedule,
    pub receiver: Schedule,
    pub sender_tid: i32,
    pub receiver_tid: i32,
    pub terminal_quiescence_verified: bool,
    pub counts: Counts,
    pub checksum: u64,
    pub retained_checksum: u64,
    pub park_returns: u32,
    pub peer_ready_verified: bool,
    pub provider_schedules: Vec<(i32, Schedule)>,
}
impl Report {
    pub fn new(config: Config) -> Self {
        Self {
            samples: Vec::with_capacity(config.samples as usize),
            sender: Schedule::default(),
            receiver: Schedule::default(),
            sender_tid: 0,
            receiver_tid: 0,
            terminal_quiescence_verified: false,
            counts: Counts::default(),
            checksum: 0,
            retained_checksum: 0,
            park_returns: 0,
            peer_ready_verified: false,
            provider_schedules: Vec::new(),
        }
    }
}
