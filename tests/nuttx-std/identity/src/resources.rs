//! Test-only native resource samples. Budget policy remains in Rust.
#[cfg(target_os = "nuttx")]
#[allow(unsafe_code)]
#[path = "ffi/resources.rs"]
mod abi;

#[derive(Clone, Copy, Debug)]
pub struct Sample {
    pub stack_used: u64,
    pub stack_capacity: u64,
    pub heap_arena: u64,
    pub heap_used: u64,
    pub heap_free: u64,
    pub heap_peak: u64,
    pub heap_largest_free: u64,
    pub accounted_thread_cpu_ns: u64,
}
impl Sample {
    fn checked(values: [u64; 8]) -> Self {
        let [stack_used, stack_capacity, heap_arena, heap_used, heap_free, heap_peak, heap_largest_free, accounted_thread_cpu_ns] =
            values;
        assert!(stack_used > 0 && stack_used < stack_capacity);
        assert!(
            stack_capacity - stack_used >= 4096,
            "fixture stack reserve exhausted"
        );
        assert!(
            heap_arena > 0 && heap_used > 0 && heap_used <= heap_peak && heap_peak <= heap_arena
        );
        assert_eq!(heap_used.checked_add(heap_free), Some(heap_arena));
        assert!(heap_largest_free <= heap_free);
        Self {
            stack_used,
            stack_capacity,
            heap_arena,
            heap_used,
            heap_free,
            heap_peak,
            heap_largest_free,
            accounted_thread_cpu_ns,
        }
    }
    pub fn read() -> Self {
        #[cfg(target_os = "nuttx")]
        {
            Self::checked(abi::read().expect("actual native resource sample"))
        }
        #[cfg(not(target_os = "nuttx"))]
        {
            panic!("native resource sample must execute inside NuttX")
        }
    }
    pub fn after(self, before: Self) -> Self {
        assert_eq!(self.stack_capacity, before.stack_capacity);
        assert_eq!(self.heap_arena, before.heap_arena);
        assert!(self.stack_used >= before.stack_used && self.heap_peak >= before.heap_peak);
        assert!(self.accounted_thread_cpu_ns > before.accounted_thread_cpu_ns);
        self
    }
    pub fn report(self, name: &str, before: Self) {
        let value = self.after(before);
        println!("NXRS_TLS_RESOURCES {name} stack_used={} stack_capacity={} heap_arena={} heap_used={} heap_free={} heap_peak={} heap_largest_free={} accounted_thread_cpu_ns={} heap_before={}",
            value.stack_used, value.stack_capacity, value.heap_arena, value.heap_used,
            value.heap_free, value.heap_peak, value.heap_largest_free,
            value.accounted_thread_cpu_ns - before.accounted_thread_cpu_ns, before.heap_used);
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn inconsistent_native_observations_or_lost_stack_reserve_refuse() {
        let good = [1024, 16384, 65536, 32768, 32768, 40000, 16000, 100];
        let before = Sample::checked(good);
        Sample::checked([2048, 16384, 65536, 30000, 35536, 41000, 16000, 200]).after(before);
        for (index, value) in [
            (0, 0),
            (0, 14000),
            (1, 1024),
            (2, 0),
            (3, 50000),
            (4, 0),
            (5, 70000),
            (6, 40000),
        ] {
            let mut changed = good;
            changed[index] = value;
            assert!(std::panic::catch_unwind(|| Sample::checked(changed)).is_err());
        }
        assert!(std::panic::catch_unwind(|| before.after(before)).is_err());
    }
}
