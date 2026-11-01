use crate::DetectionSnapshot;
use detection_wire as wire;

/// Reusable, bounded serialization storage; no allocation in the polling loop.
pub(crate) struct Publication {
    bytes: Box<[u8]>,
    length: usize,
    current: Option<wire::Source>,
    localization: bool,
}
impl Publication {
    pub fn new(localization: bool) -> Result<Self, detection_service::Error> {
        let mut bytes = Vec::new();
        bytes
            .try_reserve_exact(wire::MAX_SOURCE_BYTES)
            .map_err(|_| detection_service::Error::Configuration)?;
        bytes.resize(wire::MAX_SOURCE_BYTES, 0);
        Ok(Self {
            bytes: bytes.into_boxed_slice(),
            length: 0,
            current: None,
            localization,
        })
    }
    pub fn metadata(&self) -> Option<stream_service::Metadata<'_>> {
        let current = self.current.as_ref()?;
        (self.length > 0).then_some(stream_service::Metadata {
            revision: current.publication_id,
            content_type: wire::METADATA_TYPE,
            bytes: &self.bytes[..self.length],
        })
    }
    pub fn update(&mut self, s: DetectionSnapshot, now: u64) {
        let presence = match s.presence {
            detection_service::Presence::Unavailable => wire::Presence::Unavailable,
            detection_service::Presence::Clear => wire::Presence::Clear,
            detection_service::Presence::Person => wire::Presence::Person,
        };
        if self.current.as_ref().is_some_and(|v| {
            v.presence == presence
                && v.event_count == s.event_count
                && v.latest.as_ref().map(|o| o.frame_sequence) == s.latest.map(|o| o.sequence)
        }) {
            return;
        }
        let mut next = wire::Source {
            api_version: wire::Version::V1,
            publication_id: self
                .current
                .as_ref()
                .map_or(1, |v| v.publication_id.saturating_add(1)),
            emitted_ms: now,
            presence,
            localization: self.localization,
            latest: None,
            event_count: s.event_count,
            events: Default::default(),
        };
        if let Some(o) = s.latest {
            let mut objects = wire::Bounded::default();
            for d in o.objects.iter() {
                let b = d.bounds;
                let _ = objects.push(wire::Object {
                    class: wire::Class::Person,
                    score_permille: d.score.permille(),
                    bounds: wire::Bounds {
                        left: b.left,
                        top: b.top,
                        right: b.right,
                        bottom: b.bottom,
                    },
                });
            }
            next.latest = Some(wire::Observation {
                frame_sequence: o.sequence,
                captured_ms: o.captured_ms,
                inference_us: o.inference_us,
                person_score_permille: o.score.permille(),
                objects,
                truncated: o.objects.truncated,
            });
        }
        let mut events = s.events;
        events.sort_unstable_by_key(|v| v.map(|e| e.id).unwrap_or(u64::MAX));
        for e in events.into_iter().flatten() {
            let kind = match e.event {
                detection_service::Event::PersonEntered => wire::EventKind::PersonEntered,
                detection_service::Event::PersonLeft => wire::EventKind::PersonLeft,
                detection_service::Event::Unavailable => wire::EventKind::Unavailable,
            };
            let _ = next.events.push(wire::Event {
                id: e.id,
                at_ms: e.at_ms,
                kind,
            });
        }
        // Invalid output is never published as clear. The gateway expires the prior state.
        self.length = next.encode(&mut self.bytes).unwrap_or(0);
        self.current = Some(next);
    }
}
