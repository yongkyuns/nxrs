use crate::{native::Schedule, support::Config};

#[derive(Debug)]
pub struct TimerSample {
    pub plan_ns: u64,
    pub deadline_ns: u64,
    pub provider_resume_ns: u64,
    pub provider_resume_ticks: u32,
    pub provider_wait_observed_ns: u64,
    pub owner_wait_observed_ns: u64,
    pub provider_wait_verified: bool,
    pub expired: bool,
    pub handler_start_ticks: u32,
    pub handler_end_ticks: u32,
}

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
    pub planned_release_ns: Option<u64>,
    pub release_observed_ns: Option<u64>,
    pub timer: Option<TimerSample>,
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
            planned_release_ns: None,
            release_observed_ns: None,
            timer: None,
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
    pub release_epoch_ns: Option<u64>,
    pub owner_release_ticks: Option<u32>,
    pub owner_hold_verified: bool,
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
            release_epoch_ns: None,
            owner_release_ticks: None,
            owner_hold_verified: false,
        }
    }
}
