//! The camera, gateway and viewer intentionally implement this versioned protocol.
//! No HAL, model, network listener or platform selection belongs in this crate.
#![forbid(unsafe_code)]
use serde::{
    de::{self, SeqAccess, Visitor},
    ser::SerializeSeq,
    Deserialize, Deserializer, Serialize, Serializer,
};
use std::{fmt, marker::PhantomData};

pub const MAX_SOURCE_BYTES: usize = 4096;
pub const METADATA_TYPE: &str = "application/vnd.nxrs.detection+json";
pub const MAX_AGE_MS: u64 = 5000;
pub const SOURCE_TIMEOUT_MS: u64 = 1500;
pub const MAX_VIDEO_BYTES: usize = 4608;
pub const VIDEO_SEI_UUID: [u8; 16] = *b"nxrs-video-v1\0\0\0";

/// In-band metadata belongs to the encoded frame carrying it. It does not
/// assume that SSE arrival time equals video playout time.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VideoFrame {
    pub api_version: Version,
    pub epoch: String,
    pub frame_sequence: u64,
    pub frame_capture_ms: u64,
    pub source: Option<Source>,
}
impl VideoFrame {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.epoch.len() != 32
            || !self
                .epoch
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            || self.frame_sequence == 0
        {
            return Err("invalid video identity");
        }
        if let Some(s) = &self.source {
            s.validate()?;
            if s.emitted_ms > self.frame_capture_ms.saturating_add(1000)
                || s.latest.as_ref().is_some_and(|o| {
                    o.frame_sequence > self.frame_sequence || o.captured_ms > self.frame_capture_ms
                })
            {
                return Err("future video observation");
            }
        }
        Ok(())
    }
    /// H.264 user_data_unregistered SEI, including Annex-B start code and RBSP
    /// escaping. Insert after the access-unit delimiter, before its VCL slices.
    pub fn sei(&self) -> Result<Vec<u8>, &'static str> {
        self.validate()?;
        let json = serde_json::to_vec(self).map_err(|_| "invalid video metadata")?;
        if json.len() > MAX_VIDEO_BYTES {
            return Err("video metadata capacity");
        }
        let mut rbsp = Vec::with_capacity(json.len() + 40);
        rbsp.push(5);
        let mut length = VIDEO_SEI_UUID.len() + json.len();
        while length >= 255 {
            rbsp.push(255);
            length -= 255;
        }
        rbsp.push(length as u8);
        rbsp.extend(VIDEO_SEI_UUID);
        rbsp.extend(json);
        rbsp.push(128);
        let mut nal = Vec::with_capacity(rbsp.len() + 32);
        nal.extend([0, 0, 0, 1, 6]);
        let mut zeros = 0;
        for b in rbsp {
            if zeros >= 2 && b <= 3 {
                nal.push(3);
                zeros = 0;
            }
            nal.push(b);
            zeros = if b == 0 { zeros + 1 } else { 0 };
        }
        Ok(nal)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Version {
    #[serde(rename = "1")]
    V1,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Presence {
    Unavailable,
    Clear,
    Person,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Class {
    Person,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EventKind {
    PersonEntered,
    PersonLeft,
    Unavailable,
}

/// Fixed storage, encoded as a JSON list. Reject excess elements during decode.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Bounded<T, const N: usize>(pub [Option<T>; N]);
impl<T, const N: usize> Default for Bounded<T, N> {
    fn default() -> Self {
        Self(std::array::from_fn(|_| None))
    }
}
impl<T, const N: usize> Bounded<T, N> {
    pub fn iter(&self) -> impl Iterator<Item = &T> {
        self.0.iter().flatten()
    }
    pub fn push(&mut self, value: T) -> Result<(), &'static str> {
        let slot = self
            .0
            .iter_mut()
            .find(|v| v.is_none())
            .ok_or("list capacity")?;
        *slot = Some(value);
        Ok(())
    }
}
impl<T: Serialize, const N: usize> Serialize for Bounded<T, N> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut seq = serializer.serialize_seq(Some(self.iter().count()))?;
        for v in self.iter() {
            seq.serialize_element(v)?;
        }
        seq.end()
    }
}
impl<'de, T: Deserialize<'de>, const N: usize> Deserialize<'de> for Bounded<T, N> {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct List<T, const N: usize>(PhantomData<T>);
        impl<'de, T: Deserialize<'de>, const N: usize> Visitor<'de> for List<T, N> {
            type Value = Bounded<T, N>;
            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                write!(f, "at most {N} elements")
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut a: A) -> Result<Self::Value, A::Error> {
                let mut out = Bounded::default();
                for slot in &mut out.0 {
                    match a.next_element()? {
                        Some(v) => *slot = Some(v),
                        None => return Ok(out),
                    }
                }
                if a.next_element::<de::IgnoredAny>()?.is_some() {
                    return Err(de::Error::custom("list capacity"));
                }
                Ok(out)
            }
        }
        d.deserialize_seq(List::<T, N>(PhantomData))
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Bounds {
    pub left: u16,
    pub top: u16,
    pub right: u16,
    pub bottom: u16,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Object {
    pub class: Class,
    pub score_permille: u16,
    pub bounds: Bounds,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Observation {
    pub frame_sequence: u64,
    pub captured_ms: u64,
    pub inference_us: u64,
    pub person_score_permille: u16,
    pub objects: Bounded<Object, 10>,
    pub truncated: bool,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Event {
    pub id: u64,
    pub at_ms: u64,
    pub kind: EventKind,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Source {
    pub api_version: Version,
    pub publication_id: u64,
    pub emitted_ms: u64,
    pub presence: Presence,
    pub localization: bool,
    pub latest: Option<Observation>,
    pub event_count: u64,
    pub events: Bounded<Event, 8>,
}
impl Source {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.publication_id == 0 {
            return Err("invalid publication");
        }
        if let Some(o) = &self.latest {
            if o.frame_sequence == 0
                || o.captured_ms > self.emitted_ms
                || o.person_score_permille > 1000
                || (!self.localization && (o.objects.iter().next().is_some() || o.truncated))
            {
                return Err("invalid observation");
            }
            for v in o.objects.iter() {
                let b = v.bounds;
                if v.score_permille > 1000
                    || b.left >= b.right
                    || b.top >= b.bottom
                    || b.right > 1000
                    || b.bottom > 1000
                {
                    return Err("invalid box");
                }
            }
        } else if self.presence != Presence::Unavailable {
            return Err("missing observation");
        }
        let mut previous = 0;
        for e in self.events.iter() {
            if e.id <= previous || e.id > self.event_count || e.at_ms > self.emitted_ms {
                return Err("invalid event");
            }
            previous = e.id;
        }
        if self.events.iter().last().map(|e| e.id).unwrap_or(0) != self.event_count {
            return Err("missing events");
        }
        Ok(())
    }
    pub fn encode(&self, output: &mut [u8]) -> Result<usize, &'static str> {
        self.validate()?;
        let capacity = output.len();
        let mut tail = output;
        serde_json::to_writer(&mut tail, self).map_err(|_| "metadata capacity")?;
        Ok(capacity - tail.len())
    }
    pub fn decode(bytes: &[u8]) -> Result<Self, &'static str> {
        if bytes.len() > MAX_SOURCE_BYTES {
            return Err("metadata capacity");
        }
        let v: Self = serde_json::from_slice(bytes).map_err(|_| "invalid metadata")?;
        v.validate()?;
        Ok(v)
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Update {
    pub source_session_id: String,
    pub frame_sequence: u64,
    pub frame_capture_ms: u64,
    pub source: Source,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Snapshot {
    pub api_version: Version,
    pub camera_id: String,
    pub source_session_id: Option<String>,
    pub revision: u64,
    pub source_online: bool,
    pub presence: Presence,
    pub stream_elapsed_ms: u64,
    /// Source-owner age advanced by monotonic time since gateway receipt, not sensor/UTC age.
    pub observation_age_ms: Option<u64>,
    pub valid_for_ms: u64,
    /// True only after source-to-render timing has been independently qualified.
    pub frame_synchronized: bool,
    pub source: Option<Source>,
    pub event_gap: bool,
}
