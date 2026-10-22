//! Closed workload cases. Instrumentation is independently enabled by `probe`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Workload {
    Payload,
    Wire,
    PacketWire,
    PacketCopied,
    PacketInPlace,
}
impl Workload {
    pub const fn wire(self) -> bool {
        matches!(self, Self::Wire | Self::PacketWire)
    }
    pub const fn packet(self) -> bool {
        matches!(
            self,
            Self::PacketWire | Self::PacketCopied | Self::PacketInPlace
        )
    }
    pub const fn in_place(self) -> bool {
        matches!(self, Self::PacketInPlace)
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Io {
    Owned,
    Borrowed,
}
#[derive(Clone, Copy, Debug)]
pub struct Experiment {
    pub workload: Workload,
    pub io: Io,
    pub paired: bool,
}
impl Experiment {
    pub fn parse(name: &str) -> Result<Self, String> {
        let (workload, io, paired) = match name {
            "payload" => (Workload::Payload, Io::Borrowed, false),
            "rust-owned-z" => (Workload::Wire, Io::Owned, false),
            "rust-borrowed-z" | "rust-borrowed-2" => (Workload::Wire, Io::Borrowed, false),
            "rust-packet-wire-2" => (Workload::PacketWire, Io::Borrowed, false),
            "rust-packet-2" => (Workload::PacketCopied, Io::Borrowed, false),
            "rust-packet-inplace-2" => (Workload::PacketInPlace, Io::Borrowed, false),
            "paired-wire" => (Workload::Wire, Io::Borrowed, true),
            "paired-packet" => (Workload::PacketCopied, Io::Borrowed, true),
            "paired-packet-inplace" => (Workload::PacketInPlace, Io::Borrowed, true),
            _ => return Err(format!("unknown footprint experiment: {name}")),
        };
        Ok(Self {
            workload,
            io,
            paired,
        })
    }
}
