//! Bounded, heap-specific reconstruction of committed allocator geometry.
//!
//! This validates a collector's journal, not the collector's completeness. The
//! latter needs target qualification. Chunk geometry conveys neither allocation
//! lifetimes nor source ownership. Other heaps and task stacks remain untouched.

use crate::{
    AddressDomain, AddressRange, BuildIdentity, Error, MemoryModel, RegionKind, Result, Span,
    SpanKind, TimingRange, TimingTrace,
};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::fmt;
use std::io::Read;
use std::path::Path;

const MAX_JOURNAL_BYTES: usize = 32 * 1024 * 1024;
const MAX_RECORDS: usize = 4096;
const MAX_CHUNKS: usize = 16_384;
const MAX_RETAINED_CHUNKS: usize = 1_000_000;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct HeapJournal {
    pub schema_version: u32,
    #[serde(deserialize_with = "deserialize_build_identity")]
    pub build: BuildIdentity,
    pub provenance: ReplayProvenance,
    pub recording_id: String,
    pub clock: String,
    pub heap: ReplayHeap,
    pub initial: HeapCheckpoint,
    pub records: Vec<HeapRecord>,
    /// An absent receipt is an unclosed capture, not permission to hold the last
    /// known state indefinitely. A syntactically truncated JSON file is rejected.
    pub closing: Option<HeapCoverageReceipt>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ReplayProvenance {
    pub nuttx_revision: String,
    pub config_sha256: String,
    /// Describes the producer. Arbitrary text does not confer qualification.
    pub collector: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ReplayHeap {
    pub id: String,
    pub epoch: String,
    pub region: String,
    pub start: u64,
    pub size: u64,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum HeapChunkState {
    Allocated,
    Free,
    Reserved,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct HeapChunk {
    pub start: u64,
    pub size: u64,
    pub state: HeapChunkState,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct HeapCheckpoint {
    pub sequence: u64,
    pub time_ns: u64,
    pub chunks: Vec<HeapChunk>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum HeapRecord {
    Transaction {
        sequence: u64,
        heap_epoch: String,
        start_ns: u64,
        commit_ns: u64,
        start: u64,
        size: u64,
        expected: Vec<HeapChunk>,
        replacement: Vec<HeapChunk>,
        #[serde(default)]
        label: String,
    },
    Checkpoint {
        heap_epoch: String,
        sequence: u64,
        time_ns: u64,
        chunks: Vec<HeapChunk>,
        #[serde(default)]
        label: String,
    },
    /// Detection time does not locate the first missing mutation. Knowledge is
    /// discarded after the preceding proven sequence boundary.
    Loss {
        heap_epoch: String,
        time_ns: u64,
        reason: String,
    },
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct HeapCoverageReceipt {
    pub heap_epoch: String,
    pub sequence: u64,
    pub time_ns: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReplayCursor {
    /// Absolute source-clock nanoseconds, not a normalized trace coordinate.
    Time(u64),
    Sequence(u64),
    /// A stable index in `HeapReplay::points`; distinguishes safe observations
    /// sharing a sequence and lets the UI navigate equal source-clock ticks.
    Point(usize),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HeapEvidence {
    ObservedCheckpoint,
    Reconstructed,
}

#[derive(Clone, Debug)]
pub struct ReplayPoint {
    pub id: usize,
    pub sequence: u64,
    pub time_ns: u64,
    pub evidence: HeapEvidence,
    pub label: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HeapUnknownReason {
    UnknownHeap,
    BeforeCapture,
    AfterCoverage,
    UnclosedCapture,
    MutationInProgress,
    AmbiguousTimestamp,
    AmbiguousSequence,
    UnknownSequence,
    SequenceGap,
    SequenceOrder,
    ClockOrder,
    EpochMismatch,
    Contradiction,
    InvalidTopology,
    Loss,
    InvalidClosing,
}

impl fmt::Display for HeapUnknownReason {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::UnknownHeap => "heap is not covered by this journal",
            Self::BeforeCapture => "before the initial observation",
            Self::AfterCoverage => "after the closing coverage boundary",
            Self::UnclosedCapture => "capture has no closing coverage receipt",
            Self::MutationInProgress => "allocator mutation in progress",
            Self::AmbiguousTimestamp => "source-clock tick contains multiple ordered states",
            Self::AmbiguousSequence => "sequence identifies more than one observed geometry",
            Self::UnknownSequence => "no proven state for this sequence or point",
            Self::SequenceGap => "missing allocator sequence",
            Self::SequenceOrder => "duplicate or reordered allocator sequence",
            Self::ClockOrder => "allocator source-clock order is contradictory",
            Self::EpochMismatch => "heap epoch changed",
            Self::Contradiction => "old-chunk precondition does not match",
            Self::InvalidTopology => "chunk replacement does not tile the affected range",
            Self::Loss => "collector reported loss",
            Self::InvalidClosing => "closing receipt does not match the retained sequence",
        })
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReplayCoverageKind {
    Reconstructed,
    Mutation,
    Unknown(HeapUnknownReason),
}

#[derive(Clone, Debug)]
pub struct ReplayCoverage {
    /// Half-open source-clock interval. Exact sequence points are retained
    /// separately; a loss interval does not erase its preceding proven point.
    pub range: TimingRange,
    pub kind: ReplayCoverageKind,
    pub point_id: Option<usize>,
    pub detail: String,
}

#[derive(Clone, Debug)]
pub struct HeapReplay {
    pub journal: HeapJournal,
    pub points: Vec<ReplayPoint>,
    pub coverage: Vec<ReplayCoverage>,
    pub bounds: TimingRange,
    pub warnings: Vec<String>,
    states: Vec<Vec<HeapChunk>>,
    mutation_starts: BTreeSet<u64>,
    closed: bool,
    tail_reason: HeapUnknownReason,
}

#[derive(Debug)]
pub enum HeapState<'a> {
    Known(KnownHeapState<'a>),
    Unknown(UnknownHeapState),
}

#[derive(Debug)]
pub struct KnownHeapState<'a> {
    pub heap: &'a ReplayHeap,
    pub chunks: &'a [HeapChunk],
    pub sequence: u64,
    pub time_ns: u64,
    pub boundary_time_ns: u64,
    pub evidence: HeapEvidence,
    pub coverage: TimingRange,
    pub point_id: usize,
}

#[derive(Debug)]
pub struct UnknownHeapState {
    pub reason: HeapUnknownReason,
    pub detail: String,
    pub range: Option<TimingRange>,
}

impl KnownHeapState<'_> {
    /// One heap arena plus its exact committed chunks. No stack, allocation
    /// lifetime, caller, or source attribution is inferred from these extents.
    pub fn spans(&self) -> Vec<Span> {
        let prefix = format!(
            "replay/{}/{}/{}",
            self.heap.id, self.sequence, self.point_id
        );
        let arena = format!("{prefix}/arena");
        let mut spans = Vec::with_capacity(self.chunks.len() + 1);
        spans.push(Span {
            id: arena.clone(),
            name: self.heap.id.clone(),
            region: self.heap.region.clone(),
            start: self.heap.start,
            size: self.heap.size,
            kind: SpanKind::HeapArena,
            source: None,
            parent: None,
            detail: Some(format!(
                "Heap epoch {}; sequence {}; geometry only",
                self.heap.epoch, self.sequence
            )),
            backtrace: vec![],
        });
        spans.extend(self.chunks.iter().map(|chunk| Span {
            id: format!("{prefix}/{:x}", chunk.start),
            name: format!("{:?} chunk at 0x{:x}", chunk.state, chunk.start),
            region: self.heap.region.clone(), start: chunk.start, size: chunk.size,
            kind: match chunk.state { HeapChunkState::Allocated => SpanKind::Allocation, HeapChunkState::Free => SpanKind::Free, HeapChunkState::Reserved => SpanKind::Reserved },
            source: None, parent: Some(arena.clone()),
            detail: Some(format!("{:?}; sequence {}; chunk geometry only; application allocation lifetime and source unknown", self.evidence, self.sequence)),
            backtrace: vec![],
        }));
        spans
    }
}

pub fn load_heap_journal(
    path: &Path,
    model: &MemoryModel,
    trace: Option<&TimingTrace>,
) -> Result<HeapReplay> {
    let mut bytes = Vec::new();
    std::fs::File::open(path)?
        .take(MAX_JOURNAL_BYTES as u64 + 1)
        .read_to_end(&mut bytes)?;
    let text =
        std::str::from_utf8(&bytes).map_err(|_| replay_error("journal must be UTF-8 JSON"))?;
    parse_heap_journal(text, model, trace)
}

pub fn parse_heap_journal(
    text: &str,
    model: &MemoryModel,
    trace: Option<&TimingTrace>,
) -> Result<HeapReplay> {
    if text.len() > MAX_JOURNAL_BYTES {
        return Err(replay_error("journal exceeds 32 MiB input bound"));
    }
    let journal: HeapJournal = serde_json::from_str(text)?;
    HeapReplay::new(journal, model, trace)
}

fn replay_error(message: impl fmt::Display) -> Error {
    Error(format!("heap replay: {message}"))
}

fn deserialize_build_identity<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> std::result::Result<BuildIdentity, D::Error> {
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct StrictBuild {
        elf_sha256: String,
        #[serde(default)]
        label: String,
    }
    let build = StrictBuild::deserialize(deserializer)?;
    Ok(BuildIdentity {
        elf_sha256: build.elf_sha256,
        label: build.label,
    })
}

struct ReplayBuilder {
    points: Vec<ReplayPoint>,
    states: Vec<Vec<HeapChunk>>,
    coverage: Vec<ReplayCoverage>,
    retained_chunks: usize,
}

impl ReplayBuilder {
    fn point(
        &mut self,
        sequence: u64,
        time_ns: u64,
        chunks: Vec<HeapChunk>,
        evidence: HeapEvidence,
        label: String,
    ) -> Result<usize> {
        self.retained_chunks = self
            .retained_chunks
            .checked_add(chunks.len())
            .filter(|count| *count <= MAX_RETAINED_CHUNKS)
            .ok_or_else(|| replay_error("journal exceeds one million retained chunk states"))?;
        let id = self.points.len();
        self.points.push(ReplayPoint {
            id,
            sequence,
            time_ns,
            evidence,
            label,
        });
        self.states.push(chunks);
        Ok(id)
    }

    fn interval(
        &mut self,
        start_ns: u64,
        end_ns: u64,
        kind: ReplayCoverageKind,
        point_id: Option<usize>,
        detail: String,
    ) {
        if start_ns < end_ns {
            self.coverage.push(ReplayCoverage {
                range: TimingRange { start_ns, end_ns },
                kind,
                point_id,
                detail,
            });
        }
    }

    fn bridge(&mut self, point_id: usize, end_ns: u64) {
        self.interval(
            self.points[point_id].time_ns,
            end_ns,
            ReplayCoverageKind::Reconstructed,
            Some(point_id),
            "Contiguous retained mutation sequence covers this interval".into(),
        );
    }

    fn lost(&mut self, point_id: usize, end_ns: u64, loss: &(HeapUnknownReason, String)) {
        self.interval(
            self.points[point_id].time_ns,
            end_ns,
            ReplayCoverageKind::Unknown(loss.0),
            None,
            loss.1.clone(),
        );
    }
}

impl HeapReplay {
    /// Validate identity, address mapping and every replacement. A producer's
    /// source/configuration receipt is retained; proving that its hooks cover
    /// the actual allocator is a separate qualification gate.
    pub fn new(
        journal: HeapJournal,
        model: &MemoryModel,
        trace: Option<&TimingTrace>,
    ) -> Result<Self> {
        validate_header(&journal, model, trace)?;
        let heap_range = AddressRange::new(journal.heap.start, journal.heap.size)?;
        validate_tiling(&journal.initial.chunks, heap_range)?;
        validate_runtime_geometry(model, &journal.heap, &journal.initial.chunks)?;
        let mut builder = ReplayBuilder {
            points: vec![],
            states: vec![],
            coverage: vec![],
            retained_chunks: 0,
        };
        let mut frontier = builder.point(
            0,
            journal.initial.time_ns,
            journal.initial.chunks.clone(),
            HeapEvidence::ObservedCheckpoint,
            "Initial checkpoint".into(),
        )?;
        let mut mutation_starts = BTreeSet::new();
        let mut latest_time = journal.initial.time_ns;
        let mut seen_sequence = 0u64;
        let mut loss: Option<(HeapUnknownReason, String)> = None;
        let mut warnings = vec!["Journal consistency is not proof of collector completeness; qualify the exact source, effective configuration, ELF hooks and retained sequence independently.".into(), "Replay describes one heap's committed chunk geometry. Allocation lifetimes, caller attribution and other runtime layers are not inferred.".into()];
        for (record_index, record) in journal.records.iter().enumerate() {
            match record {
                HeapRecord::Transaction {
                    sequence,
                    heap_epoch,
                    start_ns,
                    commit_ns,
                    start,
                    size,
                    expected,
                    replacement,
                    label,
                } => {
                    let problem = if heap_epoch != &journal.heap.epoch {
                        Some((
                            HeapUnknownReason::EpochMismatch,
                            "transaction belongs to another heap epoch".into(),
                        ))
                    } else if seen_sequence.checked_add(1) != Some(*sequence) {
                        let reason = if *sequence <= seen_sequence {
                            HeapUnknownReason::SequenceOrder
                        } else {
                            HeapUnknownReason::SequenceGap
                        };
                        Some((
                            reason,
                            format!("expected sequence after {seen_sequence}, received {sequence}"),
                        ))
                    } else if start_ns > commit_ns || *start_ns < latest_time {
                        Some((HeapUnknownReason::ClockOrder, format!("transaction {sequence} has window [{start_ns}, {commit_ns}] after {latest_time}")))
                    } else {
                        None
                    };
                    if heap_epoch == &journal.heap.epoch
                        && *start_ns >= journal.initial.time_ns
                        && start_ns <= commit_ns
                    {
                        mutation_starts.insert(*start_ns);
                    }
                    let candidate = if problem.is_none() && loss.is_none() {
                        replace_chunks(
                            &builder.states[frontier],
                            heap_range,
                            *start,
                            *size,
                            expected,
                            replacement,
                        )
                        .and_then(|chunks| {
                            validate_runtime_geometry(model, &journal.heap, &chunks).map_err(
                                |error| (HeapUnknownReason::InvalidTopology, error.to_string()),
                            )?;
                            Ok(chunks)
                        })
                    } else {
                        Err((
                            HeapUnknownReason::Contradiction,
                            "state is already unknown".into(),
                        ))
                    };
                    if loss.is_none() {
                        let failure = problem.or_else(|| candidate.as_ref().err().cloned());
                        if let Some(problem) = failure {
                            warnings.push(format!(
                                "Record {record_index}: {}: {}",
                                problem.0, problem.1
                            ));
                            loss = Some(problem);
                        } else {
                            builder.bridge(frontier, *start_ns);
                            builder.interval(
                                *start_ns,
                                *commit_ns,
                                ReplayCoverageKind::Mutation,
                                None,
                                format!("Allocator mutation for sequence {sequence}"),
                            );
                            frontier = builder.point(
                                *sequence,
                                *commit_ns,
                                candidate.expect("checked replacement"),
                                HeapEvidence::Reconstructed,
                                if label.is_empty() {
                                    format!("Allocator commit {sequence}")
                                } else {
                                    label.clone()
                                },
                            )?;
                        }
                    }
                    if heap_epoch == &journal.heap.epoch {
                        seen_sequence = seen_sequence.max(*sequence);
                        latest_time = latest_time.max(*start_ns).max(*commit_ns);
                    }
                }
                HeapRecord::Checkpoint {
                    heap_epoch,
                    sequence,
                    time_ns,
                    chunks,
                    label,
                } => {
                    let problem = if heap_epoch != &journal.heap.epoch {
                        Some((
                            HeapUnknownReason::EpochMismatch,
                            "checkpoint belongs to another heap epoch".into(),
                        ))
                    } else if *sequence < seen_sequence {
                        Some((
                            HeapUnknownReason::SequenceOrder,
                            format!("checkpoint sequence {sequence} precedes {seen_sequence}"),
                        ))
                    } else if *time_ns < latest_time {
                        Some((
                            HeapUnknownReason::ClockOrder,
                            format!("checkpoint time {time_ns} precedes {latest_time}"),
                        ))
                    } else {
                        validate_tiling(chunks, heap_range)
                            .and_then(|()| validate_runtime_geometry(model, &journal.heap, chunks))
                            .err()
                            .map(|error| (HeapUnknownReason::InvalidTopology, error.to_string()))
                    };
                    if let Some(problem) = problem {
                        if loss.is_none() {
                            loss = Some(problem.clone());
                        }
                        warnings.push(format!(
                            "Record {record_index}: {}: {}",
                            problem.0, problem.1
                        ));
                    } else {
                        if loss.is_none() && *sequence != seen_sequence {
                            loss = Some((HeapUnknownReason::SequenceGap, format!("checkpoint advances from sequence {seen_sequence} to {sequence} without the intervening mutations")));
                        }
                        if loss.is_none() && builder.states[frontier] != *chunks {
                            loss = Some((
                                HeapUnknownReason::Contradiction,
                                "complete checkpoint differs without an intervening mutation"
                                    .into(),
                            ));
                        }
                        if let Some(problem) = loss.take() {
                            builder.lost(frontier, *time_ns, &problem);
                        } else {
                            builder.bridge(frontier, *time_ns);
                        }
                        frontier = builder.point(
                            *sequence,
                            *time_ns,
                            chunks.clone(),
                            HeapEvidence::ObservedCheckpoint,
                            if label.is_empty() {
                                "Complete checkpoint".into()
                            } else {
                                label.clone()
                            },
                        )?;
                    }
                    if heap_epoch == &journal.heap.epoch {
                        seen_sequence = seen_sequence.max(*sequence);
                        latest_time = latest_time.max(*time_ns);
                    }
                }
                HeapRecord::Loss {
                    heap_epoch,
                    time_ns,
                    reason,
                } => {
                    let problem = if heap_epoch != &journal.heap.epoch {
                        (
                            HeapUnknownReason::EpochMismatch,
                            "loss report belongs to another heap epoch".into(),
                        )
                    } else {
                        (HeapUnknownReason::Loss, reason.clone())
                    };
                    warnings.push(format!(
                        "Record {record_index}: {}: {}",
                        problem.0, problem.1
                    ));
                    if loss.is_none() {
                        loss = Some(problem);
                    }
                    if heap_epoch == &journal.heap.epoch {
                        latest_time = latest_time.max(*time_ns);
                    }
                }
            }
        }
        let mut closed = false;
        let mut tail_reason = HeapUnknownReason::UnclosedCapture;
        if let Some(closing) = &journal.closing {
            if loss.is_none()
                && closing.heap_epoch == journal.heap.epoch
                && closing.sequence == seen_sequence
                && closing.time_ns >= latest_time
            {
                builder.bridge(frontier, closing.time_ns);
                closed = true;
                tail_reason = HeapUnknownReason::AfterCoverage;
            } else if loss.is_none() {
                loss = Some((HeapUnknownReason::InvalidClosing, "closing epoch, final sequence, or time does not agree with the retained stream".into()));
            }
            latest_time = latest_time.max(closing.time_ns);
        }
        if let Some(problem) = loss {
            builder.lost(frontier, latest_time, &problem);
            tail_reason = problem.0;
        }
        if !closed {
            warnings.push(format!(
                "No state is held after the last proven boundary: {tail_reason}."
            ));
        }
        let bounds = TimingRange {
            start_ns: journal.initial.time_ns,
            end_ns: latest_time,
        };
        Ok(Self {
            journal,
            points: builder.points,
            coverage: builder.coverage,
            bounds,
            warnings,
            states: builder.states,
            mutation_starts,
            closed,
            tail_reason,
        })
    }

    pub fn heap_state_at(&self, heap_id: &str, cursor: ReplayCursor) -> HeapState<'_> {
        if heap_id != self.journal.heap.id {
            return unknown(
                HeapUnknownReason::UnknownHeap,
                "Select the heap declared by this journal",
                None,
            );
        }
        match cursor {
            ReplayCursor::Point(id) => self.points.get(id).map_or_else(
                || {
                    unknown(
                        HeapUnknownReason::UnknownSequence,
                        "Point index is outside the retained journal",
                        None,
                    )
                },
                |point| {
                    self.known(
                        point.id,
                        point.time_ns,
                        point.evidence,
                        TimingRange {
                            start_ns: point.time_ns,
                            end_ns: point.time_ns,
                        },
                    )
                },
            ),
            ReplayCursor::Sequence(sequence) => {
                let mut matches = self
                    .points
                    .iter()
                    .filter(|point| point.sequence == sequence);
                let Some(first) = matches.next() else {
                    return unknown(
                        HeapUnknownReason::UnknownSequence,
                        format!("No proven state at sequence {sequence}"),
                        None,
                    );
                };
                let mut selected = first;
                for point in matches {
                    if self.states[first.id] != self.states[point.id] {
                        return unknown(HeapUnknownReason::AmbiguousSequence, "Use an explicit observation point to distinguish conflicting observations at this sequence", None);
                    }
                    selected = point;
                }
                self.known(
                    selected.id,
                    selected.time_ns,
                    selected.evidence,
                    TimingRange {
                        start_ns: selected.time_ns,
                        end_ns: selected.time_ns,
                    },
                )
            }
            ReplayCursor::Time(time_ns) => self.at_time(time_ns),
        }
    }

    fn at_time(&self, time_ns: u64) -> HeapState<'_> {
        if time_ns < self.bounds.start_ns {
            return unknown(
                HeapUnknownReason::BeforeCapture,
                "The journal has no earlier observation",
                None,
            );
        }
        if time_ns > self.bounds.end_ns {
            return unknown(
                self.tail_reason,
                "No complete mutation coverage exists at this time",
                None,
            );
        }
        let mut points = self.points.iter().filter(|point| point.time_ns == time_ns);
        let first = points.next();
        let mut selected = first;
        if let Some(first) = first {
            for point in points {
                if first.sequence != point.sequence
                    || self.states[first.id] != self.states[point.id]
                {
                    return unknown(HeapUnknownReason::AmbiguousTimestamp, "Use a sequence or an explicit observation point; time alone cannot order this tick", Some(TimingRange { start_ns: time_ns, end_ns: time_ns }));
                }
                selected = Some(point);
            }
        }
        if self.mutation_starts.contains(&time_ns) {
            return unknown(HeapUnknownReason::MutationInProgress, "A mutation begins in this source-clock tick; use an explicit sequence point for the committed boundary", Some(TimingRange { start_ns: time_ns, end_ns: time_ns }));
        }
        if let Some(point) = selected {
            return self.known(
                point.id,
                time_ns,
                point.evidence,
                TimingRange {
                    start_ns: time_ns,
                    end_ns: time_ns,
                },
            );
        }
        if let Some(coverage) = self
            .coverage
            .iter()
            .find(|coverage| coverage.range.start_ns <= time_ns && time_ns < coverage.range.end_ns)
        {
            return match coverage.kind {
                ReplayCoverageKind::Reconstructed => self.known(
                    coverage.point_id.expect("known interval point"),
                    time_ns,
                    HeapEvidence::Reconstructed,
                    coverage.range,
                ),
                ReplayCoverageKind::Mutation => unknown(
                    HeapUnknownReason::MutationInProgress,
                    coverage.detail.clone(),
                    Some(coverage.range),
                ),
                ReplayCoverageKind::Unknown(reason) => {
                    unknown(reason, coverage.detail.clone(), Some(coverage.range))
                }
            };
        }
        if self.closed && time_ns == self.bounds.end_ns {
            let point = self.points.last().expect("initial checkpoint");
            return self.known(
                point.id,
                time_ns,
                HeapEvidence::Reconstructed,
                TimingRange {
                    start_ns: point.time_ns,
                    end_ns: time_ns,
                },
            );
        }
        unknown(
            self.tail_reason,
            "No complete mutation coverage exists at this time",
            None,
        )
    }

    fn known(
        &self,
        point_id: usize,
        time_ns: u64,
        evidence: HeapEvidence,
        coverage: TimingRange,
    ) -> HeapState<'_> {
        let point = &self.points[point_id];
        HeapState::Known(KnownHeapState {
            heap: &self.journal.heap,
            chunks: &self.states[point_id],
            sequence: point.sequence,
            time_ns,
            boundary_time_ns: point.time_ns,
            evidence,
            coverage,
            point_id,
        })
    }
}

fn unknown(
    reason: HeapUnknownReason,
    detail: impl Into<String>,
    range: Option<TimingRange>,
) -> HeapState<'static> {
    HeapState::Unknown(UnknownHeapState {
        reason,
        detail: detail.into(),
        range,
    })
}

fn validate_header(
    journal: &HeapJournal,
    model: &MemoryModel,
    trace: Option<&TimingTrace>,
) -> Result<()> {
    if journal.schema_version != 1 {
        return Err(replay_error("unsupported journal schema_version"));
    }
    journal.build.validate()?;
    model.validate()?;
    if !journal.build.matches(&model.build) {
        return Err(replay_error(
            "journal ELF SHA-256 differs from the loaded firmware",
        ));
    }
    if journal.records.len() > MAX_RECORDS {
        return Err(replay_error("journal exceeds 4096 records"));
    }
    if journal.initial.sequence != 0 {
        return Err(replay_error(
            "initial checkpoint must establish sequence zero",
        ));
    }
    crate::correlation::validate_recording_id(&journal.recording_id)?;
    for (name, value) in [
        ("heap id", &journal.heap.id),
        ("heap epoch", &journal.heap.epoch),
        ("heap region", &journal.heap.region),
        ("clock", &journal.clock),
        ("collector", &journal.provenance.collector),
    ] {
        if value.is_empty()
            || value.len() > 256
            || !value.bytes().all(|byte| byte.is_ascii_graphic())
        {
            return Err(replay_error(format!(
                "{name} must contain 1 to 256 printable ASCII characters without whitespace"
            )));
        }
    }
    if !hex_digest(&journal.provenance.nuttx_revision, 40)
        || !hex_digest(&journal.provenance.config_sha256, 64)
    {
        return Err(replay_error("provenance requires a 40-digit NuttX revision and 64-digit effective-configuration SHA-256"));
    }
    let region = model
        .regions
        .iter()
        .find(|region| region.id == journal.heap.region)
        .ok_or_else(|| replay_error("journal heap region is absent from the loaded address map"))?;
    if region.domain != AddressDomain::CpuVirtual
        || region.kind == RegionKind::Flash
        || !region
            .range()?
            .contains(AddressRange::new(journal.heap.start, journal.heap.size)?)
    {
        return Err(replay_error(
            "journal heap must fit inside one CPU-address RAM or unknown region",
        ));
    }
    // Independent point observations stay independent. Explicitly correlated
    // observations, however, must not silently acquire a different run/clock.
    for identity in model
        .snapshots
        .iter()
        .filter_map(|snapshot| snapshot.correlation.as_ref())
    {
        if identity.recording_id != journal.recording_id || identity.clock != journal.clock {
            return Err(replay_error(
                "journal differs from the explicitly correlated memory recording or clock",
            ));
        }
    }
    if let Some(trace) = trace {
        if trace.metadata.recording_id.as_deref() != Some(journal.recording_id.as_str())
            || trace.metadata.clock.as_deref() != Some(journal.clock.as_str())
            || !trace
                .metadata
                .elf_sha256
                .as_ref()
                .is_some_and(|hash| hash.eq_ignore_ascii_case(&journal.build.elf_sha256))
        {
            return Err(replay_error(
                "trace must identify the same ELF, recording and source clock as the heap journal",
            ));
        }
        let origin = trace.metadata.origin_ns.ok_or_else(|| {
            replay_error("trace origin_ns is required to map the heap source clock")
        })?;
        if origin > journal.initial.time_ns || origin.checked_add(trace.bounds.end_ns).is_none() {
            return Err(replay_error(
                "trace origin or extent cannot represent the heap source clock",
            ));
        }
    }
    Ok(())
}

fn hex_digest(value: &str, length: usize) -> bool {
    value.len() == length && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn validate_runtime_geometry(
    model: &MemoryModel,
    heap: &ReplayHeap,
    chunks: &[HeapChunk],
) -> Result<()> {
    let state = KnownHeapState {
        heap,
        chunks,
        sequence: 0,
        time_ns: 0,
        boundary_time_ns: 0,
        evidence: HeapEvidence::Reconstructed,
        coverage: TimingRange::default(),
        point_id: 0,
    };
    model.validate_runtime_spans(&state.spans())
}

fn validate_tiling(chunks: &[HeapChunk], range: AddressRange) -> Result<()> {
    if chunks.is_empty() || chunks.len() > MAX_CHUNKS {
        return Err(replay_error("chunk list must contain 1 to 16384 extents"));
    }
    let mut cursor = range.start;
    for chunk in chunks {
        let interval = AddressRange::new(chunk.start, chunk.size)?;
        if interval.start != cursor || !range.contains(interval) {
            return Err(replay_error(
                "chunks must tile their range in address order without gaps or overlaps",
            ));
        }
        cursor = interval.end;
    }
    if cursor != range.end {
        return Err(replay_error(
            "chunks do not cover the complete declared range",
        ));
    }
    Ok(())
}

fn replace_chunks(
    current: &[HeapChunk],
    heap: AddressRange,
    start: u64,
    size: u64,
    expected: &[HeapChunk],
    replacement: &[HeapChunk],
) -> std::result::Result<Vec<HeapChunk>, (HeapUnknownReason, String)> {
    let invalid = |error: Error| (HeapUnknownReason::InvalidTopology, error.to_string());
    let range = AddressRange::new(start, size).map_err(invalid)?;
    if !heap.contains(range) {
        return Err((
            HeapUnknownReason::InvalidTopology,
            "affected range is outside the heap".into(),
        ));
    }
    validate_tiling(expected, range).map_err(invalid)?;
    validate_tiling(replacement, range).map_err(invalid)?;
    let Some(first) = current.iter().position(|chunk| chunk.start == start) else {
        return Err((
            HeapUnknownReason::Contradiction,
            "affected range does not begin at an existing chunk boundary".into(),
        ));
    };
    let Some(end) = first
        .checked_add(expected.len())
        .filter(|end| *end <= current.len())
    else {
        return Err((
            HeapUnknownReason::Contradiction,
            "expected chunks exceed the current topology".into(),
        ));
    };
    if current[first..end] != *expected {
        return Err((
            HeapUnknownReason::Contradiction,
            "expected old chunks differ in address, extent or state".into(),
        ));
    }
    if current.len() - expected.len() + replacement.len() > MAX_CHUNKS {
        return Err((
            HeapUnknownReason::InvalidTopology,
            "replacement exceeds 16384 chunk state bound".into(),
        ));
    }
    let mut next = Vec::with_capacity(current.len() - expected.len() + replacement.len());
    next.extend_from_slice(&current[..first]);
    next.extend_from_slice(replacement);
    next.extend_from_slice(&current[end..]);
    Ok(next)
}
