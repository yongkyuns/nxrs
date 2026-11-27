//! Point observations of task stacks. Reservation, pointer depth, and touched
//! coloration are separate facts; none establishes a continuous task lifetime.

use crate::{AddressDomain, AddressRange, Error, Region, Result, Snapshot, Span, SpanKind};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum StackGrowth {
    Down,
    Up,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum StackPointerSource {
    /// A hardware register attributed to this task at the stopped checkpoint.
    LiveRegister,
    /// The task's saved context; it is not the selected CPU's live register.
    SavedContext,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(tag = "status", rename_all = "snake_case", deny_unknown_fields)]
pub enum StackCurrent {
    Observed { sp: u64, source: StackPointerSource },
    Unavailable { reason: String },
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(tag = "status", rename_all = "snake_case", deny_unknown_fields)]
pub enum StackHighWater {
    /// A read-only scan of a declared, initialized coloration interval. The
    /// watermark describes touched memory, not the maximum stack-pointer depth.
    /// Alignment can leave edges of the usable reservation outside this scan.
    Coloration {
        scan_start: u64,
        scan_size: u64,
        used_bytes: u64,
        pattern: u32,
        word_bytes: u8,
    },
    Unavailable {
        reason: String,
    },
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct StackObservation {
    /// A Stack span in this snapshot, not a global task or allocation identity.
    pub stack: String,
    pub task_id: u32,
    pub tcb: u64,
    pub growth: StackGrowth,
    pub current: StackCurrent,
    pub high_water: StackHighWater,
}

impl StackObservation {
    pub fn current_used(&self, stack: &Span) -> Option<u64> {
        let StackCurrent::Observed { sp, .. } = self.current else {
            return None;
        };
        let end = stack.start.checked_add(stack.size)?;
        if !(stack.start..=end).contains(&sp) {
            return None;
        }
        Some(match self.growth {
            StackGrowth::Down => end - sp,
            StackGrowth::Up => sp - stack.start,
        })
    }

    /// Remaining usable reservation according to the observed pointer. This
    /// does not predict future depth or prove that stack guards are intact.
    pub fn current_headroom(&self, stack: &Span) -> Option<u64> {
        stack.size.checked_sub(self.current_used(stack)?)
    }

    pub fn watermark_used(&self) -> Option<u64> {
        match self.high_water {
            StackHighWater::Coloration { used_bytes, .. } => Some(used_bytes),
            StackHighWater::Unavailable { .. } => None,
        }
    }

    /// Untouched colored bytes in the scanned interval only. Unscanned edge
    /// bytes and a missing scan are never presented as spare stack capacity.
    pub fn watermark_headroom(&self) -> Option<u64> {
        match self.high_water {
            StackHighWater::Coloration {
                scan_size,
                used_bytes,
                ..
            } => scan_size.checked_sub(used_bytes),
            StackHighWater::Unavailable { .. } => None,
        }
    }

    pub fn current_range(&self, stack: &Span) -> Option<AddressRange> {
        let size = self.current_used(stack)?;
        let start = match self.growth {
            StackGrowth::Down => stack.start.checked_add(stack.size)?.checked_sub(size)?,
            StackGrowth::Up => stack.start,
        };
        AddressRange::new(start, size).ok()
    }

    pub fn watermark_range(&self) -> Option<AddressRange> {
        let StackHighWater::Coloration {
            scan_start,
            scan_size,
            used_bytes,
            ..
        } = self.high_water
        else {
            return None;
        };
        if used_bytes > scan_size {
            return None;
        }
        let start = match self.growth {
            StackGrowth::Down => scan_start.checked_add(scan_size)?.checked_sub(used_bytes)?,
            StackGrowth::Up => scan_start,
        };
        AddressRange::new(start, used_bytes).ok()
    }
}

pub(crate) fn validate_observations(snapshot: &Snapshot, regions: &[Region]) -> Result<()> {
    let spans: BTreeMap<_, _> = snapshot
        .spans
        .iter()
        .map(|span| (span.id.as_str(), span))
        .collect();
    let mut stacks = BTreeSet::new();
    let mut tasks = BTreeSet::new();
    let mut tcbs = BTreeSet::new();
    for observation in &snapshot.stack_observations {
        let fail = |reason| Error(format!("stack observation {}: {reason}", observation.stack));
        if !stacks.insert(observation.stack.as_str())
            || !tasks.insert(observation.task_id)
            || !tcbs.insert(observation.tcb)
        {
            return Err(fail("duplicate stack, task ID, or TCB within one snapshot"));
        }
        if observation.tcb == 0 {
            return Err(fail("TCB must be a nonzero address"));
        }
        let stack = spans
            .get(observation.stack.as_str())
            .ok_or_else(|| fail("missing stack reservation"))?;
        if stack.kind != SpanKind::Stack
            || !regions.iter().any(|region| {
                region.id == stack.region && region.domain == AddressDomain::CpuVirtual
            })
        {
            return Err(fail(
                "requires a task stack reservation in a CPU address region",
            ));
        }
        match &observation.current {
            StackCurrent::Observed { .. } if observation.current_used(stack).is_none() => {
                return Err(fail("stack pointer is outside the usable reservation"));
            }
            StackCurrent::Unavailable { reason } if reason.trim().is_empty() => {
                return Err(fail("unavailable current depth requires a reason"));
            }
            _ => {}
        }
        match &observation.high_water {
            StackHighWater::Coloration {
                scan_start,
                scan_size,
                used_bytes,
                word_bytes,
                ..
            } => {
                if *word_bytes != 4 {
                    return Err(fail("version 1 coloration requires 32-bit words"));
                }
                let range = AddressRange::new(*scan_start, *scan_size)?;
                if !stack.range()?.contains(range) {
                    return Err(fail("coloration scan exceeds the usable reservation"));
                }
                if scan_start % 4 != 0 || scan_size % 4 != 0 || used_bytes % 4 != 0 {
                    return Err(fail(
                        "coloration geometry must be aligned to its 32-bit words",
                    ));
                }
                if used_bytes > scan_size {
                    return Err(fail("touched watermark exceeds the scanned interval"));
                }
                // A pointer can reserve bytes without touching them. The
                // coloration watermark need not exceed the current SP depth.
            }
            StackHighWater::Unavailable { reason } if reason.trim().is_empty() => {
                return Err(fail("unavailable coloration requires a reason"));
            }
            _ => {}
        }
    }
    Ok(())
}
