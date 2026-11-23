use super::{Failure, Result};
use detection_service::wire::VideoFrame;
use std::collections::VecDeque;

const MAX_PENDING: usize = 32;

#[derive(Default)]
pub struct Pending {
    pictures: VecDeque<(i64, VideoFrame)>,
    previous: Option<(i64, u64, u64, String)>,
}

impl Pending {
    pub fn push(&mut self, pts_ms: i64, json: &[u8]) -> Result<()> {
        let pts_ms = pts_ms
            .checked_add(1)
            .filter(|pts| *pts > 0)
            .ok_or(Failure::Media)?;
        let frame = VideoFrame::decode(json).map_err(|_| Failure::Media)?;
        if self.pictures.len() == MAX_PENDING {
            return Err(Failure::Media);
        }
        if let Some((pts, sequence, capture_ms, epoch)) = &self.previous {
            let capture_step = frame.frame_capture_ms.checked_sub(*capture_ms);
            if pts_ms <= *pts
                || frame.frame_sequence <= *sequence
                || &frame.epoch != epoch
                || capture_step != Some((pts_ms - pts) as u64)
            {
                return Err(Failure::Media);
            }
        }
        self.previous = Some((
            pts_ms,
            frame.frame_sequence,
            frame.frame_capture_ms,
            frame.epoch.clone(),
        ));
        self.pictures.push_back((pts_ms, frame));
        Ok(())
    }

    pub fn take(&mut self, pts_ms: i64) -> Result<Vec<u8>> {
        if self.pictures.front().is_none_or(|(pts, _)| *pts != pts_ms) {
            return Err(Failure::Media);
        }
        let (_, frame) = self.pictures.pop_front().expect("matched picture");
        frame.sei().map_err(|_| Failure::Media)
    }

    pub fn finish(&self) -> Result<()> {
        self.pictures.is_empty().then_some(()).ok_or(Failure::Media)
    }
}

/// Offset of the first VCL NAL in a four-byte length-prefixed AVC access unit.
pub fn insertion_point(packet: &[u8]) -> Result<usize> {
    let mut offset = 0;
    let mut first_slice = None;
    while offset < packet.len() {
        let header = packet.get(offset..offset + 4).ok_or(Failure::Media)?;
        let length = u32::from_be_bytes(header.try_into().expect("four bytes")) as usize;
        let start = offset + 4;
        let end = start.checked_add(length).ok_or(Failure::Media)?;
        let nal = packet.get(start..end).ok_or(Failure::Media)?;
        let kind = nal.first().ok_or(Failure::Media)? & 31;
        if matches!(kind, 1 | 5) {
            first_slice.get_or_insert(offset);
        }
        offset = end;
    }
    first_slice.ok_or(Failure::Media)
}

#[cfg(test)]
mod tests {
    use super::*;
    use detection_service::wire::Version;

    #[test]
    fn only_the_exact_queued_picture_can_consume_an_annotation() {
        let mut pending = Pending::default();
        let mut frame = VideoFrame {
            api_version: Version::V1,
            epoch: "0".repeat(32),
            frame_sequence: 4,
            frame_capture_ms: 100,
            source: None,
        };
        let first = frame.encode().unwrap();
        pending.push(399, &first).unwrap();
        assert_eq!(pending.push(399, &first), Err(Failure::Media));
        frame.frame_sequence += 2;
        frame.frame_capture_ms += 61;
        let second = frame.encode().unwrap();
        assert_eq!(pending.push(459, &second), Err(Failure::Media));
        pending.push(460, &second).unwrap();
        assert_eq!(pending.take(461), Err(Failure::Media));
        assert_eq!(
            pending.take(400),
            Ok(VideoFrame::decode(&first).unwrap().sei().unwrap())
        );
        assert_eq!(pending.take(400), Err(Failure::Media));
        assert_eq!(pending.finish(), Err(Failure::Media));
        assert_eq!(pending.take(461), Ok(frame.sei().unwrap()));
        assert_eq!(pending.finish(), Ok(()));
    }
}
