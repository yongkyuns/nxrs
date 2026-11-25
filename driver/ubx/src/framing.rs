//! Retain small UBX messages and reduce NAV-SAT blocks as they arrive.
use kinavis_ubx::{Frame, SYNC_1, SYNC_2};
use peripheral::gnss::SatelliteStatus;

const NAV_SAT: [u8; 2] = [1, 0x35];
const SAT_HEADER: u16 = 8;
const SAT_BLOCK: u16 = 12;
const MAX_SAT_PAYLOAD: u16 = SAT_HEADER + u8::MAX as u16 * SAT_BLOCK;
pub(super) const MAX_SAT_FRAME_BYTES: u16 = MAX_SAT_PAYLOAD + 8;

pub(super) enum Event<'a> {
    Frame(Frame<'a>),
    Satellites(u32, SatelliteStatus),
    Invalid,
}

#[derive(Default)]
enum State {
    #[default]
    Sync,
    SyncSecond,
    Header,
    Payload,
    ChecksumA,
    ChecksumB,
}

#[derive(Default)]
struct Signals {
    sum: u16,
    reported: u8,
    tracked: u8,
    cn0: u8,
}

pub(super) struct Framer {
    payload: [u8; 92],
    header: [u8; 4],
    at: u16,
    checksum: (u8, u8),
    checksum_a_matches: bool,
    state: State,
    signals: Signals,
}

impl Default for Framer {
    fn default() -> Self {
        Self {
            payload: [0; 92],
            header: [0; 4],
            at: 0,
            checksum: (0, 0),
            checksum_a_matches: false,
            state: State::default(),
            signals: Signals::default(),
        }
    }
}

impl Framer {
    fn length(&self) -> u16 {
        u16::from_le_bytes([self.header[2], self.header[3]])
    }

    fn is_satellite_report(&self) -> bool {
        self.header[..2] == NAV_SAT
    }

    fn checksum_byte(&mut self, byte: u8) {
        self.checksum.0 = self.checksum.0.wrapping_add(byte);
        self.checksum.1 = self.checksum.1.wrapping_add(self.checksum.0);
    }

    pub(super) fn push(&mut self, byte: u8) -> Option<Event<'_>> {
        match self.state {
            State::Sync if byte == SYNC_1 => self.state = State::SyncSecond,
            State::Sync => {}
            State::SyncSecond if byte == SYNC_2 => {
                self.checksum = (0, 0);
                self.at = 0;
                self.state = State::Header;
            }
            State::SyncSecond if byte != SYNC_1 => self.state = State::Sync,
            State::SyncSecond => {}
            State::Header => {
                self.header[usize::from(self.at)] = byte;
                self.checksum_byte(byte);
                self.at += 1;
                if self.at == self.header.len() as u16 {
                    let length = self.length();
                    let valid_length = if self.is_satellite_report() {
                        self.signals = Signals::default();
                        (SAT_HEADER..=MAX_SAT_PAYLOAD).contains(&length)
                            && (length - SAT_HEADER).is_multiple_of(SAT_BLOCK)
                    } else {
                        usize::from(length) <= self.payload.len()
                    };
                    if !valid_length {
                        self.state = State::Sync;
                        return Some(Event::Invalid);
                    }
                    self.at = 0;
                    self.state = if length == 0 {
                        State::ChecksumA
                    } else {
                        State::Payload
                    };
                }
            }
            State::Payload => {
                self.checksum_byte(byte);
                if self.at < SAT_HEADER || !self.is_satellite_report() {
                    self.payload[usize::from(self.at)] = byte;
                } else {
                    match (self.at - SAT_HEADER) % SAT_BLOCK {
                        2 => self.signals.cn0 = byte,
                        8 if byte & 7 >= 4 => {
                            self.signals.tracked += 1;
                            if self.signals.cn0 != 0 {
                                self.signals.sum += u16::from(self.signals.cn0);
                                self.signals.reported += 1;
                            }
                        }
                        _ => {}
                    }
                }
                self.at += 1;
                if self.at == self.length() {
                    self.state = State::ChecksumA;
                }
            }
            State::ChecksumA => {
                self.checksum_a_matches = byte == self.checksum.0;
                self.state = State::ChecksumB;
            }
            State::ChecksumB => {
                self.state = State::Sync;
                if !self.checksum_a_matches || byte != self.checksum.1 {
                    return Some(Event::Invalid);
                }
                if self.is_satellite_report() {
                    return Some(self.satellite_report());
                }
                return Some(Event::Frame(Frame {
                    class: self.header[0],
                    id: self.header[1],
                    payload: &self.payload[..usize::from(self.length())],
                }));
            }
        }
        None
    }

    fn satellite_report(&self) -> Event<'_> {
        let visible = self.payload[5];
        if self.payload[4] != 1 || self.length() != SAT_HEADER + u16::from(visible) * SAT_BLOCK {
            return Event::Invalid;
        }
        let readings = u16::from(self.signals.reported);
        let mean_cn0_db_hz =
            (readings > 0).then(|| ((self.signals.sum + readings / 2) / readings) as u8);
        Event::Satellites(
            u32::from_le_bytes(self.payload[..4].try_into().unwrap()),
            SatelliteStatus {
                visible,
                tracked: self.signals.tracked,
                mean_cn0_db_hz,
                age_ms: 0,
            },
        )
    }
}
