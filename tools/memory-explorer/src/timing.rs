//! A strict, integer-nanosecond subset of Chrome Trace Event JSON.
//!
//! Chrome JSON `ts` and `dur` are microseconds, including decimal fractions;
//! `displayTimeUnit` changes presentation, not that wire unit. Decimal numbers
//! are converted directly to integers, never through an f64 timestamp. Supported
//! phases are X, nested B/E, i/I, C, and M process/thread names. Missing duration
//! endpoints are errors. Importers should encode censored observations as instant
//! markers with explicit evidence metadata instead of inventing an end time.

use crate::{Error, Result};
use serde::Deserialize;
use serde_json::{Map, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

const MAX_TRACE_BYTES: usize = 128 * 1024 * 1024;
const MAX_TRACE_EVENTS: usize = 1_000_000;
const MAX_NESTING: usize = 1024;

fn category_filter(filter: &str) -> Option<&str> {
    filter
        .strip_prefix("cat:")
        .or_else(|| filter.strip_prefix("category:"))
        .map(str::trim)
}

fn outcome_filter(filter: &str) -> Option<&str> {
    filter.strip_prefix("outcome:").map(str::trim)
}

fn category_matches(category: &str, filter: &str) -> bool {
    !filter.is_empty()
        && category
            .split(',')
            .any(|part| part.trim().eq_ignore_ascii_case(filter))
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct TimingRange {
    pub start_ns: u64,
    pub end_ns: u64,
}

impl TimingRange {
    pub fn span_ns(self) -> u64 {
        self.end_ns.saturating_sub(self.start_ns)
    }

    /// Point markers include both selected endpoints; interval durations remain
    /// half-open and have zero overlap with a zero-width selection.
    pub fn contains(self, time_ns: u64) -> bool {
        self.start_ns <= time_ns && time_ns <= self.end_ns
    }

    /// Occurrence counts use half-open windows so adjacent buckets never count
    /// an instant or interval start twice. Display queries retain both edges.
    pub fn contains_start(self, time_ns: u64) -> bool {
        self.start_ns <= time_ns && time_ns < self.end_ns
    }
}

#[derive(Clone, Debug)]
pub struct TimingTrack {
    pub id: usize,
    pub pid: u64,
    pub tid: u64,
    pub name: String,
    pub process_name: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TimingEventKind {
    Interval,
    Instant,
}

#[derive(Clone, Debug)]
pub struct TimingEvent {
    pub id: usize,
    pub track: usize,
    pub start_ns: u64,
    pub end_ns: u64,
    pub depth: u32,
    pub name: String,
    pub category: String,
    pub kind: TimingEventKind,
    pub args: Value,
}

impl TimingEvent {
    pub fn duration_ns(&self) -> u64 {
        self.end_ns - self.start_ns
    }

    /// Censored edges and loss/coverage diagnostics are evidence markers, not
    /// semantic occurrences. They remain visible in ordinary display queries.
    pub fn is_observed_occurrence(&self) -> bool {
        !self
            .category
            .split(',')
            .any(|category| matches!(category.trim(), "unknown" | "loss" | "capture"))
            && self.argument("reason").is_none()
            && self
                .argument("duration_unknown")
                .is_none_or(|value| value == &Value::Bool(false))
            // Allocator endpoint records do not establish a complete call,
            // including legacy records without an explicit unknown category.
            && !(self.is_heap_call()
                && (self.kind == TimingEventKind::Instant
                    || self.argument("request_observed").and_then(Value::as_bool) == Some(false)
                    || self.result_argument("incomplete_reason").is_some()))
    }

    /// Read producer evidence, including the beginning of a paired B/E span.
    /// End arguments never silently replace its original source identity.
    pub fn argument(&self, name: &str) -> Option<&Value> {
        self.args
            .get(name)
            .or_else(|| self.args.get("begin_args").and_then(|args| args.get(name)))
    }

    /// Terminal evidence takes precedence for result fields. The ordinary
    /// argument accessor continues to preserve the original caller and input.
    pub fn result_argument(&self, name: &str) -> Option<&Value> {
        self.args
            .get("end_args")
            .and_then(|args| args.get(name))
            .or_else(|| self.argument(name))
    }

    pub fn is_irq(&self) -> bool {
        self.category
            .split(',')
            .any(|part| matches!(part.trim(), "irq" | "interrupt"))
    }

    pub fn is_heap(&self) -> bool {
        self.category.split(',').any(|part| part.trim() == "heap")
    }

    pub fn is_heap_call(&self) -> bool {
        self.is_heap()
            && (self
                .category
                .split(',')
                .any(|part| matches!(part.trim(), "request" | "result"))
                || self.argument("requested_bytes").is_some()
                || self.argument("request_id").is_some())
    }

    pub fn is_callback(&self) -> bool {
        self.is_irq() || self.category.split(',').any(|part| part.trim() == "timer")
    }

    /// A recorded/resolved code location, never an inferred device or owner.
    /// In particular, an IRQ number alone cannot identify a sensor.
    pub fn source_name(&self) -> Option<&str> {
        let fields: &[&str] = if self.is_callback() {
            &["handler_function", "handler_file", "source_label"]
        } else {
            &["caller_function", "caller_file", "source_label"]
        };
        fields.iter().find_map(|field| {
            self.argument(field)
                .and_then(Value::as_str)
                .filter(|value| !value.trim().is_empty())
        })
    }

    pub fn requested_bytes(&self) -> Option<u64> {
        (self.is_heap()
            && self.argument("request_observed").and_then(Value::as_bool) != Some(false))
        .then(|| self.argument("requested_bytes").and_then(Value::as_u64))
        .flatten()
    }

    /// Explicit allocator result, when reported. Paired B/E spans can record
    /// the result at E while retaining caller and request size from B.
    /// Neither a missing result nor an unrelated allocation implies success.
    pub fn request_failed(&self) -> Option<bool> {
        if !self.is_heap()
            || self.argument("request_observed").and_then(Value::as_bool) == Some(false)
            || self.result_argument("incomplete_reason").is_some()
        {
            return None;
        }
        // An explicit terminal outcome owns the result, including an unknown
        // or unsupported outcome. Do not let a conflicting legacy flag replace
        // it, or promote an orphan result into a correlated call.
        for args in [
            self.args.get("end_args"),
            Some(&self.args),
            self.args.get("begin_args"),
        ]
        .into_iter()
        .flatten()
        {
            if let Some(outcome) = args.get("outcome") {
                return match outcome.as_str() {
                    Some("succeeded") => Some(false),
                    Some("failed") => Some(true),
                    _ => None,
                };
            }
            if let Some(failed) = args.get("failed") {
                return failed.as_bool();
            }
        }
        None
    }

    pub fn failure_reason(&self) -> Option<&str> {
        (self.request_failed() == Some(true))
            .then(|| {
                self.result_argument("failure_reason")
                    .and_then(Value::as_str)
            })
            .flatten()
            .filter(|reason| !reason.trim().is_empty())
    }

    pub fn matches(&self, filter: &str) -> bool {
        let filter = filter.trim().to_lowercase();
        if let Some(category) = category_filter(&filter) {
            return category_matches(&self.category, category);
        }
        if let Some(outcome) = outcome_filter(&filter) {
            return self.is_heap_call()
                && match outcome {
                    "failed" => self.request_failed() == Some(true),
                    "succeeded" => self.request_failed() == Some(false),
                    "unknown" => self.request_failed().is_none(),
                    _ => false,
                };
        }
        filter.is_empty()
            || self.name.to_lowercase().contains(&filter)
            || self.category.to_lowercase().contains(&filter)
            || self.args.to_string().to_lowercase().contains(&filter)
    }

    pub fn intersects(&self, range: TimingRange) -> bool {
        if range.end_ns < range.start_ns {
            false
        } else if self.start_ns == self.end_ns {
            range.contains(self.start_ns)
        } else {
            self.start_ns < range.end_ns && range.start_ns < self.end_ns
        }
    }
}

#[derive(Clone, Debug)]
pub struct CounterSample {
    pub track: usize,
    pub time_ns: u64,
    pub name: String,
    pub category: String,
    pub series: String,
    /// Finite numeric value for plotting. `exact_value` preserves the original
    /// decimal representation for inspection when f64 cannot represent it.
    pub value: f64,
    pub exact_value: String,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CaptureStopReason {
    Duration,
    Interrupted,
}

impl CaptureStopReason {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Duration => "duration",
            Self::Interrupted => "interrupted",
        }
    }
}

/// Explicit source-clock recording bounds, including periods with no events.
/// Completeness here concerns retained capture coverage, independently of the
/// legacy metadata flag that also tracks incomplete interval edges.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CaptureWindow {
    pub start_ns: u64,
    pub end_ns: u64,
    pub requested_duration_ns: u64,
    pub stop_reason: CaptureStopReason,
    pub complete: bool,
    /// Native acquisition categories, not device ownership or source identity.
    pub categories: Vec<String>,
}

impl CaptureWindow {
    pub fn duration_ns(&self) -> u64 {
        self.end_ns - self.start_ns
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TimingRateUnavailable {
    NoCaptureWindow,
    IncompleteCapture,
    RecordLoss,
    UnknownLoss,
    EmptyWindow,
    OutsideCapture,
}

impl TimingRateUnavailable {
    pub fn description(self) -> &'static str {
        match self {
            Self::NoCaptureWindow => {
                "No explicit capture window; event extent is not an acquisition duration"
            }
            Self::IncompleteCapture => "Capture-window coverage is incomplete",
            Self::RecordLoss => "Record loss was reported; the occurrence population is incomplete",
            Self::UnknownLoss => "Record-loss status is unknown",
            Self::EmptyWindow => "The selected window has no positive duration",
            Self::OutsideCapture => "The selected window extends outside the recorded capture",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TimingRateDenominator {
    Observed { duration_ns: u64 },
    Unavailable(TimingRateUnavailable),
}

impl TimingRateDenominator {
    pub fn duration_ns(self) -> Option<u64> {
        match self {
            Self::Observed { duration_ns } => Some(duration_ns),
            Self::Unavailable(_) => None,
        }
    }

    pub fn per_second(self, count: usize) -> Option<f64> {
        self.duration_ns()
            .filter(|duration| *duration > 0)
            .map(|duration| count as f64 * 1_000_000_000.0 / duration as f64)
    }
}

/// Counts belong to interval starts and semantic instants in a half-open
/// window. Full durations describe the observed calls whose starts belong to
/// that window; overlap and union statistics are calculated separately.
#[derive(Clone, Debug, Default)]
pub struct TimingOccurrences {
    pub interval_starts: usize,
    pub instants: usize,
    pub excluded_markers: usize,
    pub complete_intervals: usize,
    pub full_duration_sum_ns: u128,
    pub longest_duration_ns: Option<u64>,
}

impl TimingOccurrences {
    fn observe(&mut self, event: &TimingEvent, range: TimingRange) {
        if !range.contains_start(event.start_ns) {
            return;
        }
        if !event.is_observed_occurrence() {
            self.excluded_markers += 1;
        } else if event.kind == TimingEventKind::Instant {
            self.instants += 1;
        } else {
            self.interval_starts += 1;
            self.complete_intervals += 1;
            let duration = event.duration_ns();
            self.full_duration_sum_ns += u128::from(duration);
            self.longest_duration_ns = Some(self.longest_duration_ns.unwrap_or(0).max(duration));
        }
    }

    pub fn mean_duration_ns(&self) -> Option<f64> {
        (self.complete_intervals > 0)
            .then(|| self.full_duration_sum_ns as f64 / self.complete_intervals as f64)
    }
}

#[derive(Clone, Debug, Default)]
pub struct TimingMetadata {
    /// Producer-assigned identity for one recording and target reset epoch.
    /// This token never establishes memory alignment without checked markers.
    pub recording_id: Option<String>,
    pub clock: Option<String>,
    pub source: Option<String>,
    /// Explicit source-clock origin of a normalized trace, if supplied.
    pub origin_ns: Option<u64>,
    pub capture_window: Option<CaptureWindow>,
    pub elf_sha256: Option<String>,
    pub lost_records: Option<u64>,
    /// A recorder may report that loss occurred without knowing its count.
    pub loss_detected: Option<bool>,
    pub complete: Option<bool>,
    pub incomplete_intervals: u64,
    /// Native recorder counts, kept distinct from derived display intervals.
    pub event_counts: BTreeMap<String, u64>,
    pub source_attribution: BTreeMap<String, u64>,
    pub source_elf_sha256: Option<String>,
    pub observed_note_count: Option<u64>,
    pub ignored_line_count: Option<u64>,
}

#[derive(Clone, Debug)]
pub struct TimingTrace {
    pub tracks: Vec<TimingTrack>,
    pub events: Vec<TimingEvent>,
    pub counters: Vec<CounterSample>,
    pub bounds: TimingRange,
    pub metadata: TimingMetadata,
    pub warnings: Vec<String>,
}

#[derive(Clone, Debug, Default)]
pub struct TimingTrackStats {
    pub track: usize,
    pub interval_count: usize,
    pub instant_count: usize,
    /// Union of clipped matching intervals on this one track. Nested intervals
    /// never count the same nanosecond twice. This is not cross-track CPU time.
    pub covered_ns: u64,
    pub clipped_interval_sum_ns: u128,
    pub longest_overlap_ns: u64,
}

#[derive(Clone, Debug)]
pub struct TimingStats {
    pub range: TimingRange,
    pub interval_count: usize,
    pub instant_count: usize,
    pub clipped_interval_sum_ns: u128,
    pub tracks: Vec<TimingTrackStats>,
    pub occurrences: TimingOccurrences,
    pub rate_denominator: TimingRateDenominator,
}

/// Matching observations grouped by track, operation and recorded code source.
/// Overlap durations are clipped to the query window and summed inclusively;
/// occurrence durations retain each complete interval's full elapsed duration.
/// Nested or overlapping tracks are not additive CPU utilization.
#[derive(Clone, Debug, Default)]
pub struct TimingSourceStats {
    pub track: usize,
    pub name: String,
    pub category: String,
    pub source: Option<String>,
    pub interval_count: usize,
    pub instant_count: usize,
    pub inclusive_ns: u128,
    pub longest_overlap_ns: u64,
    pub representative_event: usize,
    pub request_count: usize,
    pub requested_bytes: u128,
    pub failed_requests: usize,
    pub unknown_outcomes: usize,
    /// Result endpoints with no admitted matching request. They contribute no
    /// requested bytes, request count, or correlated success/failure.
    pub unpaired_results: usize,
    pub occurrences: TimingOccurrences,
}

impl TimingTrace {
    /// The producer's full acquisition window in trace coordinates. A capture
    /// window requires origin == its source start, so silent captures begin at 0.
    pub fn capture_range(&self) -> Option<TimingRange> {
        self.metadata
            .capture_window
            .as_ref()
            .map(|window| TimingRange {
                start_ns: 0,
                end_ns: window.duration_ns(),
            })
    }

    pub fn rate_denominator(&self, range: TimingRange) -> TimingRateDenominator {
        use TimingRateUnavailable as Unknown;
        let unavailable = |reason| TimingRateDenominator::Unavailable(reason);
        let Some(window) = &self.metadata.capture_window else {
            return unavailable(Unknown::NoCaptureWindow);
        };
        if self.metadata.loss_detected == Some(true)
            || self.metadata.lost_records.is_some_and(|count| count > 0)
        {
            return unavailable(Unknown::RecordLoss);
        }
        if !window.complete {
            return unavailable(Unknown::IncompleteCapture);
        }
        if self.metadata.loss_detected != Some(false) && self.metadata.lost_records != Some(0) {
            return unavailable(Unknown::UnknownLoss);
        }
        if range.end_ns <= range.start_ns {
            return unavailable(Unknown::EmptyWindow);
        }
        if range.end_ns > window.duration_ns() {
            return unavailable(Unknown::OutsideCapture);
        }
        TimingRateDenominator::Observed {
            duration_ns: range.end_ns - range.start_ns,
        }
    }

    pub fn event_matches(&self, event: &TimingEvent, filter: &str) -> bool {
        let filter = filter.trim().to_lowercase();
        if category_filter(&filter).is_some() || outcome_filter(&filter).is_some() {
            return event.matches(&filter);
        }
        let track = &self.tracks[event.track];
        event.matches(&filter)
            || track.name.to_lowercase().contains(&filter)
            || track.process_name.to_lowercase().contains(&filter)
    }

    pub fn query(&self, range: TimingRange, filter: &str) -> Vec<&TimingEvent> {
        self.events
            .iter()
            .filter(|event| event.intersects(range) && self.event_matches(event, filter))
            .collect()
    }

    /// Minimum duration applies only to intervals; instantaneous allocation,
    /// loss and checkpoint evidence stays visible at any duration threshold.
    pub fn source_stats(
        &self,
        range: TimingRange,
        filter: &str,
        minimum_ns: u64,
    ) -> Vec<TimingSourceStats> {
        let mut groups: BTreeMap<(usize, &str, &str, Option<&str>), TimingSourceStats> =
            BTreeMap::new();
        for event in self.query(range, filter) {
            if event.kind == TimingEventKind::Interval && event.duration_ns() < minimum_ns {
                continue;
            }
            let key = (
                event.track,
                event.name.as_str(),
                event.category.as_str(),
                event.source_name(),
            );
            let group = groups.entry(key).or_insert_with(|| TimingSourceStats {
                track: event.track,
                name: event.name.clone(),
                category: event.category.clone(),
                source: event.source_name().map(str::to_owned),
                representative_event: event.id,
                ..Default::default()
            });
            group.occurrences.observe(event, range);
            if event.kind == TimingEventKind::Instant {
                group.instant_count += 1;
            } else {
                let overlap = event
                    .end_ns
                    .min(range.end_ns)
                    .saturating_sub(event.start_ns.max(range.start_ns));
                group.interval_count += 1;
                group.inclusive_ns += u128::from(overlap);
                if overlap > group.longest_overlap_ns {
                    group.longest_overlap_ns = overlap;
                    group.representative_event = event.id;
                }
            }
            if let Some(bytes) = event.requested_bytes() {
                group.request_count += 1;
                group.requested_bytes += u128::from(bytes);
                match event.request_failed() {
                    Some(true) => group.failed_requests += 1,
                    Some(false) => {}
                    None => group.unknown_outcomes += 1,
                }
            }
            if event.is_heap_call()
                && event.argument("request_observed").and_then(Value::as_bool) == Some(false)
            {
                group.unpaired_results += 1;
            }
        }
        let mut groups: Vec<_> = groups.into_values().collect();
        groups.sort_by(|left, right| {
            right
                .inclusive_ns
                .cmp(&left.inclusive_ns)
                .then_with(|| right.requested_bytes.cmp(&left.requested_bytes))
                .then_with(|| {
                    (left.track, &left.name, &left.source).cmp(&(
                        right.track,
                        &right.name,
                        &right.source,
                    ))
                })
        });
        groups
    }

    pub fn counter_query(&self, range: TimingRange, filter: &str) -> Vec<&CounterSample> {
        self.counters
            .iter()
            .filter(|sample| range.contains(sample.time_ns) && self.counter_matches(sample, filter))
            .collect()
    }

    pub fn counter_matches(&self, sample: &CounterSample, filter: &str) -> bool {
        let filter = filter.trim().to_lowercase();
        if outcome_filter(&filter).is_some() {
            return false;
        }
        if let Some(category) = category_filter(&filter) {
            return category_matches(&sample.category, category);
        }
        let track = &self.tracks[sample.track];
        filter.is_empty()
            || sample.name.to_lowercase().contains(&filter)
            || sample.series.to_lowercase().contains(&filter)
            || sample.category.to_lowercase().contains(&filter)
            || track.name.to_lowercase().contains(&filter)
            || track.process_name.to_lowercase().contains(&filter)
    }

    pub fn stats(&self, range: TimingRange, filter: &str) -> TimingStats {
        self.stats_filtered(range, filter, 0)
    }

    pub fn stats_filtered(&self, range: TimingRange, filter: &str, minimum_ns: u64) -> TimingStats {
        let mut stats = TimingStats {
            range,
            interval_count: 0,
            instant_count: 0,
            clipped_interval_sum_ns: 0,
            tracks: Vec::new(),
            occurrences: TimingOccurrences::default(),
            rate_denominator: self.rate_denominator(range),
        };
        let mut by_track: BTreeMap<usize, (TimingTrackStats, Vec<(u64, u64)>)> = BTreeMap::new();
        for event in self.query(range, filter) {
            if event.kind == TimingEventKind::Interval && event.duration_ns() < minimum_ns {
                continue;
            }
            stats.occurrences.observe(event, range);
            let (track, intervals) = by_track.entry(event.track).or_insert_with(|| {
                (
                    TimingTrackStats {
                        track: event.track,
                        ..Default::default()
                    },
                    Vec::new(),
                )
            });
            if event.kind == TimingEventKind::Instant {
                track.instant_count += 1;
                stats.instant_count += 1;
            } else {
                let start = event.start_ns.max(range.start_ns);
                let end = event.end_ns.min(range.end_ns);
                let overlap = end.saturating_sub(start);
                track.interval_count += 1;
                track.clipped_interval_sum_ns += u128::from(overlap);
                track.longest_overlap_ns = track.longest_overlap_ns.max(overlap);
                stats.interval_count += 1;
                stats.clipped_interval_sum_ns += u128::from(overlap);
                if start < end {
                    intervals.push((start, end));
                }
            }
        }
        for (_, (mut track, mut intervals)) in by_track {
            intervals.sort_unstable();
            let mut active: Option<(u64, u64)> = None;
            for (start, end) in intervals {
                match active {
                    Some((left, right)) if start <= right => active = Some((left, right.max(end))),
                    Some((left, right)) => {
                        track.covered_ns += right - left;
                        active = Some((start, end));
                    }
                    None => active = Some((start, end)),
                }
            }
            if let Some((start, end)) = active {
                track.covered_ns += end - start;
            }
            stats.tracks.push(track);
        }
        stats
    }
}

pub fn load_timing(path: &Path) -> Result<TimingTrace> {
    if std::fs::metadata(path)?.len() > MAX_TRACE_BYTES as u64 {
        return Err(timing_error("input exceeds the 128 MiB host import bound"));
    }
    parse_timing(&std::fs::read_to_string(path)?)
}

pub fn parse_timing(text: &str) -> Result<TimingTrace> {
    if text.len() > MAX_TRACE_BYTES {
        return Err(timing_error("input exceeds the 128 MiB host import bound"));
    }
    let document: Value = serde_json::from_str(text)?;
    let records = document
        .as_array()
        .or_else(|| document.get("traceEvents").and_then(Value::as_array))
        .ok_or_else(|| timing_error("expected an event array or object with traceEvents array"))?;
    if records.len() > MAX_TRACE_EVENTS {
        return Err(timing_error("input exceeds one million event records"));
    }
    let (metadata, mut warnings) = parse_metadata(&document)?;
    let mut tracks = Vec::new();
    let mut track_indices = BTreeMap::new();
    let mut process_names = BTreeMap::new();
    let mut thread_names = BTreeMap::new();
    let mut events = Vec::new();
    let mut counters = Vec::new();
    let mut pending: BTreeMap<usize, Vec<TimingEvent>> = BTreeMap::new();
    let mut previous_edge = BTreeMap::new();

    for (index, record) in records.iter().enumerate() {
        let object = record
            .as_object()
            .ok_or_else(|| timing_error(format!("event {index} is not an object")))?;
        let phase = required_str(object, "ph")?;
        let pid = required_u64(object, "pid")?;
        let tid = optional_u64(object, "tid")?.unwrap_or(0);
        let args = match object.get("args") {
            None | Some(Value::Null) => Value::Object(Map::new()),
            Some(value) if value.is_object() => value.clone(),
            _ => {
                return Err(timing_error(format!(
                    "event {index}: args must be an object"
                )))
            }
        };
        if phase == "M" {
            match required_str(object, "name")? {
                "process_name" => {
                    process_names.insert(
                        pid,
                        required_str(args.as_object().unwrap(), "name")?.to_owned(),
                    );
                }
                "thread_name" => {
                    thread_names.insert(
                        (pid, tid),
                        required_str(args.as_object().unwrap(), "name")?.to_owned(),
                    );
                }
                name => warnings.push(format!("Ignored unsupported metadata event {name:?}.")),
            }
            continue;
        }
        if !matches!(phase, "X" | "B" | "E" | "i" | "I" | "C") {
            return Err(timing_error(format!(
                "unsupported phase {phase:?} at event {index}; supported: X, B/E, i/I, C, M"
            )));
        }
        let time = microseconds_ns(required(object, "ts")?)?;
        let track = *track_indices.entry((pid, tid)).or_insert_with(|| {
            let id = tracks.len();
            tracks.push(TimingTrack {
                id,
                pid,
                tid,
                name: format!("Thread {tid}"),
                process_name: format!("Process {pid}"),
            });
            id
        });
        if matches!(phase, "B" | "E")
            && previous_edge
                .insert(track, time)
                .is_some_and(|previous| time < previous)
        {
            return Err(timing_error(format!(
                "B/E timestamps run backward on pid {pid}, tid {tid}"
            )));
        }
        if phase == "E" {
            let mut begin = pending.entry(track).or_default().pop().ok_or_else(|| {
                timing_error(format!(
                    "unmatched E event at {time} ns on pid {pid}, tid {tid}"
                ))
            })?;
            if optional_text(object, "name")?
                .filter(|name| !name.is_empty())
                .is_some_and(|name| name != begin.name)
                || optional_text(object, "cat")?.is_some_and(|category| category != begin.category)
            {
                return Err(timing_error(
                    "E event name/category conflicts with its nested B event",
                ));
            }
            if time < begin.start_ns {
                return Err(timing_error("E event precedes its B event"));
            }
            begin.end_ns = time;
            if !args.as_object().unwrap().is_empty() {
                begin.args = serde_json::json!({"begin_args": begin.args, "end_args": args});
            }
            events.push(begin);
            continue;
        }
        let name = required_str(object, "name")?.to_owned();
        let category = optional_text(object, "cat")?.unwrap_or("").to_owned();
        if phase == "C" {
            if args.as_object().unwrap().is_empty() {
                return Err(timing_error(
                    "C event requires at least one numeric args series",
                ));
            }
            for (series, value) in args.as_object().unwrap() {
                let number = value
                    .as_number()
                    .ok_or_else(|| timing_error("counter values must be JSON numbers"))?;
                let numeric = number
                    .as_f64()
                    .filter(|value| value.is_finite())
                    .ok_or_else(|| timing_error("counter value exceeds finite plotting range"))?;
                counters.push(CounterSample {
                    track,
                    time_ns: time,
                    name: name.clone(),
                    category: category.clone(),
                    series: series.clone(),
                    value: numeric,
                    exact_value: number.to_string(),
                });
            }
            continue;
        }
        let kind = if matches!(phase, "i" | "I") {
            TimingEventKind::Instant
        } else {
            TimingEventKind::Interval
        };
        let end_ns = if phase == "X" {
            time.checked_add(microseconds_ns(required(object, "dur")?)?)
                .ok_or_else(|| timing_error("interval end overflows u64 nanoseconds"))?
        } else {
            time
        };
        let event = TimingEvent {
            id: index,
            track,
            start_ns: time,
            end_ns,
            depth: 0,
            name,
            category,
            kind,
            args,
        };
        if phase == "B" {
            let stack = pending.entry(track).or_default();
            if stack.len() >= MAX_NESTING {
                return Err(timing_error(
                    "B/E nesting exceeds the 1024-level host bound",
                ));
            }
            stack.push(event);
        } else {
            events.push(event);
        }
    }
    let unmatched: usize = pending.values().map(Vec::len).sum();
    if unmatched > 0 {
        return Err(timing_error(format!(
            "{unmatched} unmatched B event(s); no end time is inferred"
        )));
    }
    for track in &mut tracks {
        if let Some(name) = process_names.get(&track.pid) {
            track.process_name.clone_from(name);
        }
        if let Some(name) = thread_names.get(&(track.pid, track.tid)) {
            track.name.clone_from(name);
        }
    }
    events.sort_by_key(|event| (event.start_ns, std::cmp::Reverse(event.end_ns), event.id));
    // A synchronous Chrome track is a nested timeline, not a set of arbitrary
    // crossing async intervals. Reject ambiguity instead of inventing a stack.
    let mut nesting: BTreeMap<usize, Vec<u64>> = BTreeMap::new();
    for event in &mut events {
        let active = nesting.entry(event.track).or_default();
        while active.last().is_some_and(|end| *end <= event.start_ns) {
            active.pop();
        }
        if active.last().is_some_and(|end| event.end_ns > *end) {
            return Err(timing_error(format!(
                "crossing, non-nested intervals on track {}; use separate tracks",
                event.track
            )));
        }
        event.depth =
            u32::try_from(active.len()).map_err(|_| timing_error("nesting depth exceeds u32"))?;
        if event.kind == TimingEventKind::Interval && event.start_ns < event.end_ns {
            if active.len() >= MAX_NESTING {
                return Err(timing_error(
                    "interval nesting exceeds the 1024-level host bound",
                ));
            }
            active.push(event.end_ns);
        }
    }
    counters.sort_by(|a, b| {
        (a.time_ns, a.track, &a.name, &a.series).cmp(&(b.time_ns, b.track, &b.name, &b.series))
    });
    let observed_start = events
        .iter()
        .map(|event| event.start_ns)
        .chain(counters.iter().map(|sample| sample.time_ns))
        .min();
    let observed_end = events
        .iter()
        .map(|event| event.end_ns)
        .chain(counters.iter().map(|sample| sample.time_ns))
        .max();
    let (start_ns, end_ns) = if let Some(window) = &metadata.capture_window {
        let duration = window.duration_ns();
        if events
            .iter()
            .any(|event| event.start_ns >= duration || event.end_ns > duration)
            || counters.iter().any(|sample| sample.time_ns >= duration)
        {
            return Err(timing_error(
                "observations must lie inside the half-open capture window",
            ));
        }
        (0, duration)
    } else {
        (
            observed_start.ok_or_else(|| {
                timing_error("trace has no interval, instant, or counter observations")
            })?,
            observed_end.expect("an observed start has an observed end"),
        )
    };
    if metadata
        .origin_ns
        .is_some_and(|origin| origin.checked_add(end_ns).is_none())
    {
        return Err(timing_error(
            "source-clock origin plus trace extent overflows u64 nanoseconds",
        ));
    }
    Ok(TimingTrace {
        tracks,
        events,
        counters,
        bounds: TimingRange { start_ns, end_ns },
        metadata,
        warnings,
    })
}

fn parse_metadata(document: &Value) -> Result<(TimingMetadata, Vec<String>)> {
    let mut metadata = TimingMetadata::default();
    let mut warnings = Vec::new();
    if let Some(value) = document
        .get("otherData")
        .and_then(|value| value.get("nxrs"))
    {
        let object = value
            .as_object()
            .ok_or_else(|| timing_error("otherData.nxrs must be an object"))?;
        if optional_u64(object, "schema_version")?.is_some_and(|version| version != 1) {
            return Err(timing_error("unsupported otherData.nxrs schema_version"));
        }
        metadata.clock = optional_str(object, "clock")?.map(str::to_owned);
        metadata.recording_id = optional_str(object, "recording_id")?.map(str::to_owned);
        if let Some(recording_id) = &metadata.recording_id {
            crate::correlation::validate_recording_id(recording_id)?;
        }
        metadata.source = optional_str(object, "source")?.map(str::to_owned);
        metadata.elf_sha256 = optional_str(object, "elf_sha256")?.map(str::to_owned);
        if metadata.elf_sha256.as_ref().is_some_and(|hash| {
            hash.len() != 64 || !hash.bytes().all(|value| value.is_ascii_hexdigit())
        }) {
            return Err(timing_error(
                "metadata elf_sha256 must contain 64 hexadecimal digits",
            ));
        }
        metadata.origin_ns = optional_u64(object, "origin_ns")?;
        metadata.lost_records = optional_u64(object, "lost_records")?;
        metadata.loss_detected = optional_bool(object, "loss_detected")?;
        metadata.complete = optional_bool(object, "complete")?;
        if let Some(value) = object.get("capture_window") {
            let window: CaptureWindow = serde_json::from_value(value.clone())
                .map_err(|error| timing_error(format!("invalid capture_window: {error}")))?;
            validate_capture_window(&window, &metadata)?;
            metadata.capture_window = Some(window);
        }
        metadata.incomplete_intervals = optional_u64(object, "incomplete_intervals")?.unwrap_or(0);
        metadata.observed_note_count = optional_u64(object, "observed_note_count")?;
        metadata.ignored_line_count = optional_u64(object, "ignored_line_count")?;
        for (field, target) in [
            ("event_counts", &mut metadata.event_counts),
            ("source_attribution", &mut metadata.source_attribution),
        ] {
            if let Some(counts) = object.get(field) {
                let counts = counts
                    .as_object()
                    .ok_or_else(|| timing_error(format!("metadata {field} must be an object")))?;
                for (name, value) in counts {
                    if field == "source_attribution" && name == "elf_sha256" {
                        let hash = value.as_str().filter(|hash| hash.len() == 64 && hash.bytes().all(|byte| byte.is_ascii_hexdigit())).ok_or_else(|| timing_error("source_attribution elf_sha256 must contain 64 hexadecimal digits"))?;
                        if metadata
                            .elf_sha256
                            .as_ref()
                            .is_some_and(|elf| !elf.eq_ignore_ascii_case(hash))
                        {
                            return Err(timing_error(
                                "source attribution ELF does not match trace ELF",
                            ));
                        }
                        metadata.source_elf_sha256 = Some(hash.to_owned());
                        continue;
                    }
                    let count = value.as_u64().ok_or_else(|| {
                        timing_error(format!(
                            "metadata {field}.{name} must be a nonnegative integer"
                        ))
                    })?;
                    target.insert(name.clone(), count);
                }
            }
        }
        if metadata.loss_detected == Some(false)
            && metadata.lost_records.is_some_and(|count| count > 0)
            || metadata.loss_detected == Some(true) && metadata.lost_records == Some(0)
        {
            return Err(timing_error(
                "metadata contradicts itself about record loss",
            ));
        }
        if let Some(value) = object.get("warnings") {
            for warning in value
                .as_array()
                .ok_or_else(|| timing_error("metadata warnings must be an array"))?
            {
                warnings.push(
                    warning
                        .as_str()
                        .ok_or_else(|| timing_error("metadata warning must be a string"))?
                        .to_owned(),
                );
            }
        }
    }
    if metadata.clock.is_none() {
        warnings.push("Clock unspecified: durations are differences in supplied trace timestamps; no target clock is asserted.".into());
    }
    if metadata.source.is_none() {
        warnings.push("Capture source unspecified; this trace is not evidence of a QEMU or physical-board run.".into());
    }
    if metadata.loss_detected == Some(true) || metadata.lost_records.is_some_and(|count| count > 0)
    {
        warnings.push(
            "Record loss was reported. Missing work cannot be reconstructed from this trace."
                .into(),
        );
    } else if metadata.lost_records.is_none() && metadata.loss_detected.is_none() {
        warnings
            .push("Record-loss status is unknown; absent intervals do not prove idle time.".into());
    }
    if metadata.complete == Some(false) || metadata.incomplete_intervals > 0 {
        warnings.push(if metadata.capture_window.is_some() {
            "Some interval edges are incomplete. Censored markers have no call duration and are excluded from occurrence counts; capture-window retention is reported separately.".into()
        } else {
            "Capture has incomplete coverage. Unclosed intervals have no inferred duration.".into()
        });
    }
    if metadata.recording_id.is_none() {
        warnings.push("Timing coordinates are not synchronized with memory snapshot labels. An ELF match alone does not establish a clock mapping.".into());
    } else {
        warnings.push("Recording identity permits checkpoint verification, but does not itself synchronize memory. A link requires matching ELF, recording, clock, unique marker, and exact source timestamp; memory between checkpoints remains unknown.".into());
    }
    warnings.push("Per-track coverage is an interval union. Scheduled residency can overlap IRQ and application tracks; these layers are not additive CPU time.".into());
    Ok((metadata, warnings))
}

fn validate_capture_window(window: &CaptureWindow, metadata: &TimingMetadata) -> Result<()> {
    if window.start_ns >= window.end_ns {
        return Err(timing_error("capture_window must have positive duration"));
    }
    if window.requested_duration_ns == 0 {
        return Err(timing_error(
            "capture_window requested_duration_ns must be positive",
        ));
    }
    if window.stop_reason == CaptureStopReason::Duration
        && window.duration_ns() < window.requested_duration_ns
    {
        return Err(timing_error(
            "duration stop precedes the requested capture duration",
        ));
    }
    if metadata.origin_ns != Some(window.start_ns) {
        return Err(timing_error(
            "capture_window start_ns must equal the declared origin_ns",
        ));
    }
    if metadata
        .clock
        .as_ref()
        .is_none_or(|clock| clock.trim().is_empty())
        || metadata.recording_id.is_none()
    {
        return Err(timing_error(
            "capture_window requires a clock and recording_id",
        ));
    }
    if window.complete
        && (metadata.loss_detected == Some(true)
            || metadata.lost_records.is_some_and(|count| count > 0))
    {
        return Err(timing_error(
            "complete capture_window contradicts reported record loss",
        ));
    }
    let mut seen = BTreeSet::new();
    for category in &window.categories {
        if !matches!(
            category.as_str(),
            "switch" | "irq" | "app" | "heap" | "syscall"
        ) || !seen.insert(category)
        {
            return Err(timing_error(
                "capture_window categories must be unique native instrumentation names",
            ));
        }
    }
    Ok(())
}

/// Parse a decimal JSON microsecond number into exact whole nanoseconds.
fn microseconds_ns(value: &Value) -> Result<u64> {
    let text = value
        .as_number()
        .ok_or_else(|| timing_error("ts/dur must be JSON numbers in microseconds"))?
        .to_string();
    if text.starts_with('-') {
        return Err(timing_error(
            "negative timestamps or durations are unsupported",
        ));
    }
    let (mantissa, exponent) = match text.find(['e', 'E']) {
        Some(index) => (
            &text[..index],
            text[index + 1..]
                .parse::<i64>()
                .map_err(|_| timing_error("numeric exponent is out of range"))?,
        ),
        None => (text.as_str(), 0),
    };
    let fractional = mantissa
        .split_once('.')
        .map_or(0, |(_, fraction)| fraction.len());
    let mut digits: String = mantissa
        .chars()
        .filter(|character| *character != '.')
        .collect();
    let trimmed = digits.trim_start_matches('0');
    if trimmed.is_empty() {
        return Ok(0);
    }
    digits = trimmed.to_owned();
    let power = exponent
        .checked_add(3)
        .and_then(|value| value.checked_sub(fractional as i64))
        .ok_or_else(|| timing_error("numeric exponent is out of range"))?;
    if power >= 0 {
        if power > 20 || digits.len() as i64 + power > 20 {
            return Err(timing_error("ts/dur overflows u64 nanoseconds"));
        }
        digits.extend(std::iter::repeat_n('0', power as usize));
    } else {
        let remove = power
            .checked_neg()
            .ok_or_else(|| timing_error("numeric exponent is out of range"))?
            as u64;
        if remove > digits.len() as u64
            || !digits[digits.len() - remove as usize..]
                .bytes()
                .all(|digit| digit == b'0')
        {
            return Err(timing_error(
                "sub-nanosecond ts/dur cannot be represented exactly",
            ));
        }
        digits.truncate(digits.len() - remove as usize);
    }
    digits
        .parse()
        .map_err(|_| timing_error("ts/dur overflows u64 nanoseconds"))
}

fn timing_error(message: impl AsRef<str>) -> Error {
    Error(format!("Timing trace: {}", message.as_ref()))
}

fn required<'a>(object: &'a Map<String, Value>, name: &str) -> Result<&'a Value> {
    object
        .get(name)
        .ok_or_else(|| timing_error(format!("missing {name}")))
}

fn required_str<'a>(object: &'a Map<String, Value>, name: &str) -> Result<&'a str> {
    optional_str(object, name)?.ok_or_else(|| timing_error(format!("missing {name}")))
}

fn optional_str<'a>(object: &'a Map<String, Value>, name: &str) -> Result<Option<&'a str>> {
    match object.get(name) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) if !value.is_empty() => Ok(Some(value)),
        _ => Err(timing_error(format!("{name} must be a nonempty string"))),
    }
}

fn optional_text<'a>(object: &'a Map<String, Value>, name: &str) -> Result<Option<&'a str>> {
    match object.get(name) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => Ok(Some(value)),
        _ => Err(timing_error(format!("{name} must be a string"))),
    }
}

fn required_u64(object: &Map<String, Value>, name: &str) -> Result<u64> {
    optional_u64(object, name)?.ok_or_else(|| timing_error(format!("missing {name}")))
}

fn optional_u64(object: &Map<String, Value>, name: &str) -> Result<Option<u64>> {
    match object.get(name) {
        None | Some(Value::Null) => Ok(None),
        Some(value) => value
            .as_u64()
            .map(Some)
            .ok_or_else(|| timing_error(format!("{name} must be an unsigned integer"))),
    }
}

fn optional_bool(object: &Map<String, Value>, name: &str) -> Result<Option<bool>> {
    match object.get(name) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Bool(value)) => Ok(Some(*value)),
        _ => Err(timing_error(format!("{name} must be a boolean"))),
    }
}

pub fn timing_demo() -> TimingTrace {
    parse_timing(r#"{
      "otherData":{"nxrs":{"schema_version":1,"source":"SYNTHETIC DEMONSTRATION — no target measurement","clock":"synthetic coordinate","origin_ns":0,"lost_records":0,"loss_detected":false,"complete":true}},
      "traceEvents":[
        {"ph":"M","pid":1,"name":"process_name","args":{"name":"Synthetic CPU schedule"}},
        {"ph":"M","pid":1,"tid":0,"name":"thread_name","args":{"name":"CPU 0"}},
        {"ph":"M","pid":2,"name":"process_name","args":{"name":"Synthetic interrupts"}},
        {"ph":"M","pid":2,"tid":0,"name":"thread_name","args":{"name":"CPU 0 IRQ nesting"}},
        {"ph":"M","pid":3,"name":"process_name","args":{"name":"Synthetic application"}},
        {"ph":"M","pid":3,"tid":7,"name":"thread_name","args":{"name":"stream worker"}},
        {"ph":"X","pid":1,"tid":0,"ts":0,"dur":600,"name":"idle","cat":"sched,cpu"},
        {"ph":"X","pid":1,"tid":0,"ts":600,"dur":1500,"name":"stream worker","cat":"sched,cpu"},
        {"ph":"X","pid":1,"tid":0,"ts":2100,"dur":900,"name":"publisher","cat":"sched,cpu"},
        {"ph":"X","pid":1,"tid":0,"ts":3000,"dur":1000,"name":"idle","cat":"sched,cpu"},
        {"ph":"X","pid":2,"tid":0,"ts":540,"dur":60,"name":"GPIO IRQ","cat":"irq"},
        {"ph":"X","pid":2,"tid":0,"ts":560,"dur":10,"name":"nested timer IRQ","cat":"irq"},
        {"ph":"X","pid":2,"tid":0,"ts":1970,"dur":24.5,"name":"timer IRQ","cat":"irq"},
        {"ph":"B","pid":3,"tid":7,"ts":620,"name":"process frame","cat":"app"},
        {"ph":"B","pid":3,"tid":7,"ts":660,"name":"I2C service","cat":"app,io"},
        {"ph":"E","pid":3,"tid":7,"ts":1140},
        {"ph":"B","pid":3,"tid":7,"ts":1200,"name":"filter","cat":"app,compute"},
        {"ph":"E","pid":3,"tid":7,"ts":1820},
        {"ph":"i","pid":3,"tid":7,"ts":1900,"name":"frame ready","cat":"app","s":"t"},
        {"ph":"E","pid":3,"tid":7,"ts":2070},
        {"ph":"C","pid":3,"tid":7,"ts":0,"name":"queue depth","args":{"frames":0}},
        {"ph":"C","pid":3,"tid":7,"ts":1900,"name":"queue depth","args":{"frames":1}},
        {"ph":"C","pid":3,"tid":7,"ts":3000,"name":"queue depth","args":{"frames":0}}
      ]
    }"#).expect("the built-in synthetic timing example is valid")
}
