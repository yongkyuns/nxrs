//! Optional target-only worker and user-heap resource snapshots.
#![allow(unsafe_code)]

use std::io;

#[cfg(target_os = "nuttx")]
use std::{
    fs,
    sync::{
        atomic::{AtomicI32, Ordering},
        Mutex,
    },
    time::{Duration, Instant},
};

#[cfg(target_os = "nuttx")]
unsafe extern "C" {
    fn imu_qualification_resources_available() -> i32;
    fn imu_qualification_mount_procfs() -> i32;
    fn imu_qualification_resources(tid: i32, cpu_ns: *mut u64, heap_bytes: *mut i32) -> i32;
}

#[cfg(target_os = "nuttx")]
#[derive(Clone, Copy, Debug)]
struct Snapshot {
    elapsed: Duration,
    cpu_ns: u64,
    stack_size_bytes: u64,
    stack_used_bytes: u64,
    user_heap_current_bytes: u64,
    user_heap_peak_bytes: u64,
}

#[cfg(target_os = "nuttx")]
#[derive(Default)]
enum Attempt {
    #[default]
    Pending,
    Captured(Snapshot),
    Failed(String),
}

#[cfg(target_os = "nuttx")]
pub(crate) struct Resources {
    started: Instant,
    worker_tid: AtomicI32,
    first: Mutex<Attempt>,
    last: Mutex<Attempt>,
}

#[cfg(target_os = "nuttx")]
impl Resources {
    /// Returns `None` when the image does not enable the complete measurement
    /// configuration. If it does, setup errors are returned and must be fixed.
    pub(crate) fn open(current_tid: Option<i32>) -> io::Result<Option<Self>> {
        // SAFETY: this scalar reports whether the pinned image compiled all
        // required perf, procfs, stack coloration and UP scheduler features.
        if unsafe { imu_qualification_resources_available() } == 0 {
            return Ok(None);
        }

        // SAFETY: this binding requests only the fixed procfs mount point.
        if unsafe { imu_qualification_mount_procfs() } < 0 {
            return Err(io::Error::last_os_error());
        }
        let tid = current_tid.ok_or_else(|| io::Error::other("cannot identify init thread"))?;
        let stack = fs::read_to_string(format!("/proc/{tid}/stack"))?;
        parse_stack(&stack)?;

        Ok(Some(Self {
            started: Instant::now(),
            worker_tid: AtomicI32::new(0),
            first: Mutex::new(Attempt::Pending),
            last: Mutex::new(Attempt::Pending),
        }))
    }

    /// Called by the acquisition worker's first accepted sample callback.
    pub(crate) fn observe_worker(&self, current_tid: impl FnOnce() -> Option<i32>) {
        if self.worker_tid.load(Ordering::Relaxed) == 0 {
            if let Some(tid) = current_tid().filter(|tid| *tid > 0) {
                self.worker_tid.store(tid, Ordering::Release);
            }
        }
    }

    /// Capture exactly once, when the first event reaches the receiver.
    pub(crate) fn capture_first(&self) {
        self.store_snapshot(&self.first);
    }

    /// Capture before stopping/joining, while the worker TCB still exists.
    pub(crate) fn capture_last(&self, worker_alive: bool) {
        if !worker_alive {
            self.store_failure(
                &self.last,
                "acquisition worker exited before final snapshot".into(),
            );
            return;
        }
        self.store_snapshot(&self.last);
    }

    fn store_snapshot(&self, destination: &Mutex<Attempt>) {
        let result = self.snapshot();
        let mut state = destination
            .lock()
            .expect("resource snapshot mutex poisoned");
        match result {
            Ok(snapshot) => *state = Attempt::Captured(snapshot),
            Err(error) => *state = Attempt::Failed(error.to_string()),
        }
    }

    fn store_failure(&self, destination: &Mutex<Attempt>, error: String) {
        *destination
            .lock()
            .expect("resource snapshot mutex poisoned") = Attempt::Failed(error);
    }

    fn snapshot(&self) -> io::Result<Snapshot> {
        let tid = self.worker_tid.load(Ordering::Acquire);
        if tid <= 0 {
            return Err(io::Error::other("acquisition worker TID was not observed"));
        }
        let mut cpu_ns = 0;
        let mut heap = [0; 2];
        // SAFETY: the binding writes into valid local buffers and validates
        // the live pthread through pthread_getcpuclockid.
        if unsafe { imu_qualification_resources(tid, &mut cpu_ns, heap.as_mut_ptr()) } != 0 {
            return Err(io::Error::last_os_error());
        }

        let stack = fs::read_to_string(format!("/proc/{tid}/stack"))?;
        let (stack_size_bytes, stack_used_bytes) = parse_stack(&stack)?;
        Ok(Snapshot {
            elapsed: self.started.elapsed(),
            cpu_ns,
            stack_size_bytes,
            stack_used_bytes,
            user_heap_current_bytes: u64::try_from(heap[0])
                .map_err(|_| io::Error::other("negative NuttX user heap allocation"))?,
            user_heap_peak_bytes: u64::try_from(heap[1])
                .map_err(|_| io::Error::other("negative NuttX user heap high-water"))?,
        })
    }

    pub(crate) fn report(&self) -> bool {
        let first = self.first.lock().expect("resource snapshot mutex poisoned");
        let last = self.last.lock().expect("resource snapshot mutex poisoned");
        let pair = match (&*first, &*last) {
            (Attempt::Captured(first), Attempt::Captured(last)) => Some((*first, *last)),
            _ => None,
        };
        let metrics = pair.and_then(|(first, last)| {
            let cpu_delta_ns = progressing_delta(first.cpu_ns, last.cpu_ns)?;
            (last.elapsed > first.elapsed).then_some((first, last, cpu_delta_ns))
        });
        let valid = metrics.is_some_and(|(first, last, _)| {
            first.stack_size_bytes == last.stack_size_bytes
                && first.stack_size_bytes > 0
                && first.stack_used_bytes <= first.stack_size_bytes
                && last.stack_used_bytes <= last.stack_size_bytes
        });

        if let Some((first, last, cpu_delta_ns)) = metrics {
            println!(
                "{{\"resource_schema\":1,\"valid\":{valid},\
                 \"cpu_clock\":\"CLOCK_THREAD_CPUTIME_ID\",\
                 \"cpu_scope\":\"NuttX per-thread runtime charged between snapshots; includes task-context kernel work and may include interrupt intervals while scheduled\",\
                 \"cpu_start_ns\":{},\"cpu_end_ns\":{},\"cpu_delta_ns\":{cpu_delta_ns},\
                 \"snapshot_start_ms\":{},\"snapshot_end_ms\":{},\
                 \"stack_scope\":\"imu-acquire fill-pattern high-water through end snapshot\",\
                 \"stack_size_bytes\":{},\"stack_used_start_bytes\":{},\
                 \"stack_used_end_bytes\":{},\"stack_used_high_water_bytes\":{},\
                 \"heap_scope\":\"NuttX mallinfo arena including kernel allocations when shared, excluding static RAM\",\
                 \"user_heap_current_start_bytes\":{},\"user_heap_current_end_bytes\":{},\
                 \"user_heap_peak_bytes\":{}}}",
                first.cpu_ns,
                last.cpu_ns,
                first.elapsed.as_millis(),
                last.elapsed.as_millis(),
                last.stack_size_bytes,
                first.stack_used_bytes,
                last.stack_used_bytes,
                first.stack_used_bytes.max(last.stack_used_bytes),
                first.user_heap_current_bytes,
                last.user_heap_current_bytes,
                first.user_heap_peak_bytes.max(last.user_heap_peak_bytes),
            );
        } else {
            for (name, attempt) in [("first", &*first), ("last", &*last)] {
                match attempt {
                    Attempt::Pending => eprintln!("resource {name} snapshot was not captured"),
                    Attempt::Failed(error) => eprintln!("resource {name} snapshot failed: {error}"),
                    Attempt::Captured(_) => {}
                }
            }
            if let (Attempt::Captured(first), Attempt::Captured(last)) = (&*first, &*last) {
                if last.cpu_ns <= first.cpu_ns {
                    eprintln!("worker CPU clock did not progress monotonically");
                } else if last.elapsed <= first.elapsed {
                    eprintln!("monotonic snapshot clock did not progress");
                }
            }
            println!("{{\"resource_schema\":1,\"valid\":false}}");
        }
        valid
    }
}

fn progressing_delta(start: u64, end: u64) -> Option<u64> {
    let delta = end.checked_sub(start)?;
    (delta > 0).then_some(delta)
}

fn parse_stack(text: &str) -> io::Result<(u64, u64)> {
    let mut size = None;
    let mut used = None;
    for line in text.lines() {
        let Some((key, value)) = line.split_once(':') else {
            continue;
        };
        let key = key.trim();
        if !matches!(key, "StackSize" | "StackUsed") {
            continue;
        }
        let value = value.trim().parse::<u64>().map_err(|_| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "invalid NuttX procfs stack value",
            )
        })?;
        match key {
            "StackSize" => size = Some(value),
            "StackUsed" => used = Some(value),
            _ => {}
        }
    }
    let size = size.ok_or_else(|| io::Error::other("procfs omitted StackSize"))?;
    let used = used.ok_or_else(|| io::Error::other("procfs omitted StackUsed"))?;
    if size == 0 || used > size {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "invalid NuttX procfs stack bounds",
        ));
    }
    Ok((size, used))
}

#[cfg(test)]
mod tests {
    use super::{parse_stack, progressing_delta};

    #[test]
    fn reads_stack_high_water_from_pinned_procfs_fields() {
        assert_eq!(
            parse_stack("StackAlloc: 0x1234\nStackSize: 4096\nStackUsed: 712\n").unwrap(),
            (4096, 712)
        );
    }

    #[test]
    fn rejects_missing_or_impossible_stack_observations() {
        assert!(parse_stack("StackSize: 4096\n").is_err());
        assert!(parse_stack("StackSize: 20\nStackUsed: 21\n").is_err());
    }

    #[test]
    fn cpu_clock_delta_must_progress_without_regression() {
        assert_eq!(progressing_delta(12, 42), Some(30));
        assert_eq!(progressing_delta(42, 42), None);
        assert_eq!(progressing_delta(43, 42), None);
    }
}
