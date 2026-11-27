//! A strict, integer-nanosecond subset of Chrome Trace Event JSON.
//!
//! Chrome JSON `ts` and `dur` are microseconds, including decimal fractions;
//! `displayTimeUnit` changes presentation, not that wire unit. Decimal numbers
//! are converted directly to integers, never through an f64 timestamp. Supported
//! phases are X, nested B/E, i/I, C, and M process/thread names. Missing duration
//! endpoints are errors. Importers should encode censored observations as instant
//! markers with explicit evidence metadata instead of inventing an end time.

use crate::{Error, Result};
use serde_json::{Map, Value};
use std::collections::BTreeMap;
use std::path::Path;

const MAX_TRACE_BYTES: usize = 128 * 1024 * 1024;
const MAX_TRACE_EVENTS: usize = 1_000_000;
const MAX_NESTING: usize = 1024;

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

    pub fn matches(&self, filter: &str) -> bool {
        let filter = filter.trim().to_lowercase();
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
    pub series: String,
    /// Finite numeric value for plotting. `exact_value` preserves the original
    /// decimal representation for inspection when f64 cannot represent it.
    pub value: f64,
    pub exact_value: String,
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
    pub elf_sha256: Option<String>,
    pub lost_records: Option<u64>,
    /// A recorder may report that loss occurred without knowing its count.
    pub loss_detected: Option<bool>,
    pub complete: Option<bool>,
    pub incomplete_intervals: u64,
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
}

impl TimingTrace {
    pub fn event_matches(&self, event: &TimingEvent, filter: &str) -> bool {
        let filter = filter.trim().to_lowercase();
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

    pub fn counter_query(&self, range: TimingRange, filter: &str) -> Vec<&CounterSample> {
        let filter = filter.trim().to_lowercase();
        self.counters
            .iter()
            .filter(|sample| {
                let track = &self.tracks[sample.track];
                range.contains(sample.time_ns)
                    && (filter.is_empty()
                        || sample.name.to_lowercase().contains(&filter)
                        || sample.series.to_lowercase().contains(&filter)
                        || track.name.to_lowercase().contains(&filter)
                        || track.process_name.to_lowercase().contains(&filter))
            })
            .collect()
    }

    pub fn stats(&self, range: TimingRange, filter: &str) -> TimingStats {
        let mut stats = TimingStats {
            range,
            interval_count: 0,
            instant_count: 0,
            clipped_interval_sum_ns: 0,
            tracks: Vec::new(),
        };
        let mut by_track: BTreeMap<usize, (TimingTrackStats, Vec<(u64, u64)>)> = BTreeMap::new();
        for event in self.query(range, filter) {
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
    let start_ns = events
        .iter()
        .map(|event| event.start_ns)
        .chain(counters.iter().map(|sample| sample.time_ns))
        .min()
        .ok_or_else(|| timing_error("trace has no interval, instant, or counter observations"))?;
    let end_ns = events
        .iter()
        .map(|event| event.end_ns)
        .chain(counters.iter().map(|sample| sample.time_ns))
        .max()
        .unwrap();
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
        metadata.incomplete_intervals = optional_u64(object, "incomplete_intervals")?.unwrap_or(0);
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
        warnings.push(
            "Capture has incomplete coverage. Unclosed intervals have no inferred duration.".into(),
        );
    }
    if metadata.recording_id.is_none() {
        warnings.push("Timing coordinates are not synchronized with memory snapshot labels. An ELF match alone does not establish a clock mapping.".into());
    } else {
        warnings.push("Recording identity permits checkpoint verification, but does not itself synchronize memory. A link requires matching ELF, recording, clock, unique marker, and exact source timestamp; memory between checkpoints remains unknown.".into());
    }
    warnings.push("Per-track coverage is an interval union. Scheduled residency can overlap IRQ and application tracks; these layers are not additive CPU time.".into());
    Ok((metadata, warnings))
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
