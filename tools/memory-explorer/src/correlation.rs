//! Explicit links between stopped memory observations and trace instants.
//!
//! Equal ELF hashes, clock descriptions, or numerical timestamps do not by
//! themselves establish a shared recording. Producers must identify one reset
//! epoch and preserve its unique checkpoint markers. This module checks those
//! assertions against the supplied trace; it never reconstructs heap history.

use crate::{Error, MemoryModel, Result, Snapshot, TimingEvent, TimingEventKind, TimingTrace};
use std::collections::BTreeMap;

const MARKER_PREFIX: &str = "memory/checkpoint/";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CheckpointLink {
    pub snapshot_index: usize,
    pub snapshot_time_ns: u64,
    pub checkpoint_id: u64,
    /// Stable imported event ID, not the event's position after sorting.
    pub event_id: usize,
    /// Trace-file coordinate, after subtracting its declared source origin.
    pub trace_time_ns: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CheckpointCorrelation {
    pub recording_id: String,
    pub clock: String,
    pub origin_ns: u64,
    pub links: Vec<CheckpointLink>,
}

impl CheckpointCorrelation {
    /// Convert a trace cursor to the shared source-clock coordinate. The
    /// result can fall between observations and does not imply observed state.
    pub fn source_time_ns(&self, trace_time_ns: u64) -> Option<u64> {
        self.origin_ns.checked_add(trace_time_ns)
    }

    pub fn link_for_event(&self, event_id: usize) -> Option<&CheckpointLink> {
        self.links.iter().find(|link| link.event_id == event_id)
    }

    pub fn link_for_snapshot(&self, snapshot_time_ns: u64) -> Option<&CheckpointLink> {
        self.links
            .iter()
            .find(|link| link.snapshot_time_ns == snapshot_time_ns)
    }

    /// Return only an exactly observed checkpoint. No nearest-point holding or
    /// interpolation is allowed, including after a reported trace-loss gap.
    pub fn link_at_trace_time(&self, trace_time_ns: u64) -> Option<&CheckpointLink> {
        self.links
            .binary_search_by_key(&trace_time_ns, |link| link.trace_time_ns)
            .ok()
            .map(|index| &self.links[index])
    }
}

pub(crate) fn validate_recording_id(recording_id: &str) -> Result<()> {
    if recording_id.is_empty()
        || recording_id.len() > 128
        || !recording_id.bytes().all(|byte| byte.is_ascii_graphic())
    {
        return Err(correlation_error(
            "recording_id must contain 1 to 128 printable ASCII characters without whitespace",
        ));
    }
    Ok(())
}

pub(crate) fn validate_snapshot_correlations(snapshots: &[Snapshot]) -> Result<()> {
    let Some(first) = snapshots
        .iter()
        .find_map(|snapshot| snapshot.correlation.as_ref())
    else {
        return Ok(());
    };
    validate_recording_id(&first.recording_id)?;
    if first.clock.trim().is_empty() {
        return Err(correlation_error("snapshot clock must be nonempty"));
    }
    let mut previous_id = 0;
    let mut previous_time = None;
    for snapshot in snapshots {
        let identity = snapshot.correlation.as_ref().ok_or_else(|| {
            correlation_error("cannot mix correlated and uncorrelated memory checkpoints")
        })?;
        if identity.recording_id != first.recording_id || identity.clock != first.clock {
            return Err(correlation_error(
                "memory checkpoints must share one recording_id and clock",
            ));
        }
        if identity.checkpoint_id <= previous_id {
            return Err(correlation_error(
                "checkpoint IDs must be nonzero and strictly increasing in source-clock order",
            ));
        }
        if previous_time.is_some_and(|time| snapshot.time_ns <= time) {
            return Err(correlation_error(
                "checkpoint source timestamps must be strictly increasing",
            ));
        }
        previous_id = identity.checkpoint_id;
        previous_time = Some(snapshot.time_ns);
    }
    Ok(())
}

/// Verify explicitly claimed links. Legacy snapshots remain independent even
/// when their ELF or numerical coordinates happen to match the trace. Missing
/// or contradictory evidence for a claimed link is an error, never a fallback
/// to guessed alignment. Surviving, directly observed points remain usable
/// after trace loss; no state is inferred in the unobserved gaps.
pub fn correlate_checkpoints(
    model: &MemoryModel,
    trace: &TimingTrace,
) -> Result<Option<CheckpointCorrelation>> {
    validate_snapshot_correlations(&model.snapshots)?;
    let Some(identity) = model
        .snapshots
        .first()
        .and_then(|snapshot| snapshot.correlation.as_ref())
    else {
        return Ok(None);
    };
    let metadata = &trace.metadata;
    if !metadata
        .elf_sha256
        .as_ref()
        .is_some_and(|hash| model.build.elf_sha256.eq_ignore_ascii_case(hash))
    {
        return Err(correlation_error(
            "trace must declare the matching firmware ELF SHA-256",
        ));
    }
    if metadata.recording_id.as_deref() != Some(&identity.recording_id) {
        return Err(correlation_error(
            "trace recording_id is missing or differs from the memory recording",
        ));
    }
    if metadata.clock.as_deref() != Some(&identity.clock) {
        return Err(correlation_error(
            "trace clock is missing or differs from the memory clock",
        ));
    }
    let origin_ns = metadata.origin_ns.ok_or_else(|| {
        correlation_error("trace origin_ns is required for source-clock checkpoint mapping")
    })?;
    let mut markers: BTreeMap<u64, &TimingEvent> = BTreeMap::new();
    for event in &trace.events {
        let Some(id) = marker_id(&event.name)? else {
            continue;
        };
        if markers.insert(id, event).is_some() {
            return Err(correlation_error(format!(
                "duplicate checkpoint marker {MARKER_PREFIX}{id}",
            )));
        }
        if event.kind != TimingEventKind::Instant || event.start_ns != event.end_ns {
            return Err(correlation_error(format!(
                "checkpoint marker {MARKER_PREFIX}{id} must be an instant",
            )));
        }
    }
    // Counter samples are stored separately from ordinary events. A reserved
    // checkpoint name must not become an admissible marker through that path.
    for sample in &trace.counters {
        if marker_id(&sample.name)?.is_some() {
            return Err(correlation_error(format!(
                "checkpoint marker {} must be an instant, not a counter",
                sample.name,
            )));
        }
    }
    let mut links = Vec::with_capacity(model.snapshots.len());
    for (snapshot_index, snapshot) in model.snapshots.iter().enumerate() {
        let checkpoint_id = snapshot
            .correlation
            .as_ref()
            .expect("validated correlation")
            .checkpoint_id;
        let event = markers.get(&checkpoint_id).ok_or_else(|| {
            correlation_error(format!(
                "missing checkpoint marker {MARKER_PREFIX}{checkpoint_id}"
            ))
        })?;
        let source_time = origin_ns.checked_add(event.start_ns).ok_or_else(|| {
            correlation_error("source-clock origin plus checkpoint timestamp overflows u64")
        })?;
        if snapshot.time_ns != source_time {
            return Err(correlation_error(format!(
                "checkpoint {checkpoint_id} timestamp differs: memory {} ns, marker {source_time} ns",
                snapshot.time_ns,
            )));
        }
        links.push(CheckpointLink {
            snapshot_index,
            snapshot_time_ns: snapshot.time_ns,
            checkpoint_id,
            event_id: event.id,
            trace_time_ns: event.start_ns,
        });
    }
    Ok(Some(CheckpointCorrelation {
        recording_id: identity.recording_id.clone(),
        clock: identity.clock.clone(),
        origin_ns,
        links,
    }))
}

fn marker_id(name: &str) -> Result<Option<u64>> {
    let Some(suffix) = name.strip_prefix(MARKER_PREFIX) else {
        return Ok(None);
    };
    let id = suffix
        .parse::<u64>()
        .map_err(|_| correlation_error(format!("invalid checkpoint marker name {name:?}")))?;
    if id == 0 || suffix != id.to_string() {
        return Err(correlation_error(format!(
            "checkpoint marker must end with a canonical nonzero integer: {name:?}",
        )));
    }
    Ok(Some(id))
}

fn correlation_error(message: impl AsRef<str>) -> Error {
    Error(format!("Checkpoint correlation: {}", message.as_ref()))
}
