use super::DemoResult;
use std::borrow::Cow;
use std::cmp::Reverse;
use std::collections::{BTreeMap, BinaryHeap, HashMap, TryReserveError, VecDeque};
use std::fmt::Write as _;
use std::io::{Cursor, Read, Write};

#[cfg(all(test, feature = "probe"))]
#[allow(dead_code)] // Executed by the dedicated harness-free qualification target.
#[path = "collections_memory.rs"]
pub(super) mod memory_qualification;

/// App-local policy around std Vec, NOT an inline/fixed-storage collection.
/// The Vec is deliberately private: callers cannot bypass the logical limit.
struct BoundedVec<T> {
    values: Vec<T>,
    limit: usize,
}

impl<T> BoundedVec<T> {
    fn try_new(limit: usize) -> Result<Self, TryReserveError> {
        let mut values = Vec::new();
        values.try_reserve_exact(limit)?;
        Ok(Self { values, limit })
    }

    fn try_push(&mut self, value: T) -> Result<(), T> {
        if self.values.len() == self.limit {
            return Err(value);
        }
        self.values.push(value);
        Ok(())
    }

    fn clear(&mut self) {
        self.values.clear();
    }
}

pub(super) fn vectors() -> DemoResult {
    // Dynamic growth: do not assume a particular growth factor or allocator.
    let mut dynamic = Vec::<u32>::new();
    let mut growths = 0;
    for sample in 0..64 {
        let before = dynamic.capacity();
        dynamic.push(sample);
        growths += usize::from(dynamic.capacity() != before);
    }
    assert_eq!(dynamic.iter().sum::<u32>(), 2016);
    println!(
        "  dynamic Vec: len={} capacity={} growth_events={growths}",
        dynamic.len(),
        dynamic.capacity()
    );

    // with_capacity is a reservation, NOT a limit. Fill the ACTUAL capacity,
    // because the allocator may give more storage than was requested.
    let mut reserved = Vec::with_capacity(4);
    let initial = reserved.capacity();
    reserved.resize(initial, 7u16);
    reserved.push(8);
    assert!(reserved.capacity() > initial);
    println!(
        "  with_capacity(4): actual_initial={initial} grew_to={}",
        reserved.capacity()
    );

    // Fallible initialization + enforced logical bound + repeated reuse.
    let mut bounded = BoundedVec::<u32>::try_new(16)?;
    let capacity = bounded.values.capacity();
    let pointer = bounded.values.as_ptr();
    for frame in 0..100 {
        for sample in 0..16 {
            bounded.try_push(frame + sample).unwrap();
        }
        assert_eq!(bounded.try_push(999), Err(999));
        assert_eq!(bounded.values.capacity(), capacity);
        assert_eq!(bounded.values.as_ptr(), pointer);
        bounded.clear();
    }
    // Guaranteed capacity overflow, not deliberate physical-memory exhaustion.
    assert!(Vec::<u32>::new().try_reserve(usize::MAX).is_err());
    println!("  bounded Vec: limit=16 reuse_cycles=100 overflow=rejected storage=heap");
    Ok(())
}

pub(super) fn fixed_storage() -> DemoResult {
    // Truly inline storage using only std/core. This is an array + occupied
    // slice, not a claim that std has an inline fixed-capacity Vec type.
    let mut samples = [0u16; 4];
    let mut used = 0;
    let mut rejected = 0;
    for value in 10..15 {
        match samples.get_mut(used) {
            Some(slot) => {
                *slot = value;
                used += 1;
            }
            None => rejected += 1,
        }
    }
    assert_eq!(&samples[..used], &[10, 11, 12, 13]);
    assert_eq!(rejected, 1);
    println!(
        "  inline array: capacity=4 rejected=1 payload_bytes={}",
        std::mem::size_of_val(&samples)
    );
    Ok(())
}

pub(super) fn maps() -> DemoResult {
    // Borrowed/static keys do not allocate a String on every insertion.
    let mut counts = HashMap::<&'static str, u32>::new();
    counts.try_reserve(3)?;
    let capacity = counts.capacity();
    for key in ["imu", "gnss", "camera", "imu"] {
        *counts.entry(key).or_insert(0) += 1;
    }
    // Updating a known key is allowed at the limit; adding a new one is not.
    let update = |map: &mut HashMap<&'static str, u32>, key| {
        if !map.contains_key(key) && map.len() == 3 {
            return false;
        }
        *map.entry(key).or_insert(0) += 1;
        true
    };
    assert!(update(&mut counts, "imu"));
    assert!(!update(&mut counts, "overflow"));
    assert_eq!(counts["imu"], 3);
    assert_eq!(counts.capacity(), capacity);
    // BTreeMap gives key order, but allocates tree nodes. It is not a bounded
    // substitute for the HashMap above and is not used in the stress hot path.
    let ordered: BTreeMap<_, _> = counts.into_iter().collect();
    assert_eq!(
        ordered.keys().copied().collect::<Vec<_>>(),
        ["camera", "gnss", "imu"]
    );
    println!("  HashMap: bounded keys=3 existing-key update=yes new-key overflow=rejected");
    println!("  BTreeMap: ordered telemetry={ordered:?} (node allocations)");
    Ok(())
}

pub(super) fn queues() -> DemoResult {
    let mut window = VecDeque::new();
    window.try_reserve_exact(4)?;
    let capacity = window.capacity();
    for sample in 0..12u32 {
        if window.len() == 4 {
            window.pop_front();
        }
        window.push_back(sample);
    }
    assert_eq!(window.iter().sum::<u32>(), 38);
    assert_eq!(window.capacity(), capacity);
    let mut deadlines = BinaryHeap::new();
    deadlines.try_reserve(3)?;
    for item in [(30, "flush"), (10, "sample"), (20, "health")] {
        deadlines.push(Reverse(item));
    }
    assert_eq!(deadlines.pop(), Some(Reverse((10, "sample"))));
    assert_eq!(deadlines.pop(), Some(Reverse((20, "health"))));
    println!("  VecDeque: rolling window=[8,9,10,11]; BinaryHeap<Reverse<_>>: earliest first");
    Ok(())
}

pub(super) fn bytes_and_text() -> DemoResult {
    let mut packet = [0u8; 6];
    {
        let mut writer = Cursor::new(packet.as_mut_slice());
        writer.write_all(&42u16.to_le_bytes())?;
        writer.write_all(&1000u32.to_le_bytes())?;
        assert!(writer.write_all(&[0]).is_err());
    }
    let mut reader = Cursor::new(packet.as_slice());
    let mut id = [0u8; 2];
    reader.read_exact(&mut id)?;
    assert_eq!(u16::from_le_bytes(id), 42);

    let mut text = String::new();
    text.try_reserve(32)?;
    let capacity = text.capacity();
    for sequence in 0..10u16 {
        text.clear();
        // The bounded integer range and fixed literal fit the reservation.
        // General write!/formatting is NOT automatically capacity-limited.
        write!(&mut text, "sample={sequence}")?;
        assert_eq!(text.capacity(), capacity);
    }
    let mut label: Cow<'_, str> = Cow::Borrowed("imu");
    assert!(matches!(label, Cow::Borrowed(_)));
    label.to_mut().push_str("-0"); // This explicitly crosses into owned storage.
    assert_eq!(label, "imu-0");
    println!("  Cursor: fixed packet bounds checked; String: capacity reused; Cow: borrow -> own");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recipes() {
        vectors().unwrap();
        fixed_storage().unwrap();
        maps().unwrap();
        queues().unwrap();
        bytes_and_text().unwrap();
    }

    #[test]
    fn logical_limit_is_not_allocator_capacity() {
        let mut zero = BoundedVec::try_new(0).unwrap();
        assert_eq!(zero.try_push(7), Err(7));
        let mut one = BoundedVec::try_new(1).unwrap();
        assert_eq!(one.try_push(7), Ok(()));
        assert_eq!(one.try_push(8), Err(8));
        one.clear();
        assert_eq!(one.try_push(9), Ok(()));
    }

    #[test]
    fn rejected_owned_value_returns_to_caller() {
        let mut values = BoundedVec::try_new(0).unwrap();
        assert_eq!(
            values.try_push(String::from("retained")),
            Err(String::from("retained"))
        );
    }
}
