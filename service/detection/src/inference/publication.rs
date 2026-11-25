use super::{Error, Snapshot};
use crate::{wire, Presence};

#[derive(PartialEq, Eq)]
struct Stamp {
    presence: Presence,
    event_count: u64,
    sequence: Option<u64>,
}

/// Reusable, bounded serialization storage; no allocation in the polling loop.
pub(super) struct Publication {
    bytes: Box<[u8]>,
    length: usize,
    revision: u64,
    stamp: Option<Stamp>,
}
impl Publication {
    pub(super) fn new() -> Result<Self, Error> {
        let mut bytes = Vec::new();
        bytes
            .try_reserve_exact(wire::MAX_SOURCE_BYTES)
            .map_err(|_| Error::Configuration)?;
        bytes.resize(wire::MAX_SOURCE_BYTES, 0);
        Ok(Self {
            bytes: bytes.into_boxed_slice(),
            length: 0,
            revision: 0,
            stamp: None,
        })
    }
    pub(super) fn metadata(&self) -> Option<(u64, &[u8])> {
        (self.length > 0).then_some((self.revision, &self.bytes[..self.length]))
    }
    pub(super) fn update(&mut self, s: &Snapshot, localization: bool, now: u64) {
        let stamp = Stamp {
            presence: s.presence,
            event_count: s.event_count,
            sequence: s.latest.map(|o| o.sequence),
        };
        if self.stamp.as_ref() == Some(&stamp) {
            return;
        }
        self.revision = self.revision.saturating_add(1);
        let mut next = wire::Source {
            api_version: wire::Version::V1,
            publication_id: self.revision,
            emitted_ms: now,
            presence: s.presence,
            localization,
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
            let _ = next.events.push(e);
        }
        // Invalid output is never published as clear. The gateway expires the prior state.
        self.length = next.encode(&mut self.bytes).unwrap_or(0);
        self.stamp = Some(stamp);
    }
}

#[cfg(test)]
mod tests {
    use crate::{wire, EventKind, Input, Policy, Presence, Score, Session, WorkerOptions};
    use detector::{Capabilities, DetectionResult, Detector, DeviceError, Image};
    use std::time::{Duration, Instant};

    struct Person;
    impl Detector for Person {
        fn capabilities(&self) -> Capabilities {
            Capabilities::presence(96, 96)
        }

        fn detect(&mut self, _: Image<'_>) -> Result<DetectionResult, DeviceError> {
            Ok(DetectionResult::presence(Score::new(900)?))
        }
    }

    #[test]
    fn session_publishes_frame_identity_and_expires_without_new_frames() {
        let policy = Policy {
            confirmation_samples: 1,
            ..Policy::default()
        };
        let options = WorkerOptions {
            frame_capacity: 4,
            sample_interval_ms: 1,
            stack_bytes: 65536,
        };
        let mut session = Session::start(options, policy, || Ok(Person)).unwrap();
        let input = Input {
            image: Image::jpeg(&[0xff, 0xd8, 0xff, 0xd9], 96, 96),
            sequence: 42,
            captured_ms: 3,
        };
        let deadline = Instant::now() + Duration::from_secs(2);
        while session.snapshot().presence != Presence::Person {
            session.poll(5, Some(input));
            assert!(Instant::now() < deadline);
            std::thread::yield_now();
        }
        let (id, bytes) = session.publication().unwrap();
        let source = wire::Source::decode(bytes).unwrap();
        assert_eq!(source.publication_id, id);
        assert_eq!(source.presence, Presence::Person);
        assert_eq!(source.latest.unwrap().frame_sequence, 42);
        assert_eq!(
            source.events.iter().next().unwrap().kind,
            EventKind::PersonEntered
        );

        session.poll(6, None);
        assert_eq!(session.publication().unwrap().0, id);
        session.poll(4 + policy.max_age_ms, None);
        let (expired_id, bytes) = session.publication().unwrap();
        let source = wire::Source::decode(bytes).unwrap();
        assert_eq!(expired_id, id + 1);
        assert_eq!(source.presence, Presence::Unavailable);
        assert!(source.latest.is_none());
        assert_eq!(
            source.events.iter().last().unwrap().kind,
            EventKind::Unavailable
        );
        session.stop().unwrap();
        assert!(session.publication().is_none());
    }
}
