use serde::Serialize;

#[derive(Clone, Copy, Debug, Serialize)]
pub struct Schedule {
    pub tid: Option<i32>,
    pub policy: Option<i32>,
    pub priority: Option<i32>,
}

#[derive(Debug, Serialize)]
pub struct Participant {
    pub role: &'static str,
    pub requested_priority: i32,
    pub before: Schedule,
    pub start: Schedule,
    pub end: Schedule,
    pub restored: Schedule,
}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct MutexConfiguration {
    pub priority_inheritance: bool,
    pub default_prio_inherit: bool,
    pub default_protocol: i32,
    pub protocol_none: i32,
    pub protocol_inherit: i32,
    pub holder_capacity: i32,
}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct ThreadState {
    pub tid: i32,
    pub policy: i32,
    pub effective_priority: i32,
    pub base_priority: i32,
    pub state: i32,
    pub waiting_semaphore: bool,
    pub runnable: bool,
}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct HolderState {
    pub state: ThreadState,
    pub tracked_holders: usize,
}

#[cfg(target_os = "nuttx")]
mod native {
    unsafe extern "C" {
        pub fn owner_qualification_schedule(values: *mut i32) -> i32;
        pub fn owner_qualification_set_schedule(policy: i32, priority: i32) -> i32;
        pub fn owner_qualification_fifo() -> i32;
        pub fn owner_qualification_waiting(tid: i32, priority: i32) -> i32;
        pub fn owner_qualification_mutex_configuration(values: *mut i32) -> i32;
        pub fn owner_qualification_thread_state(tid: i32, values: *mut i32) -> i32;
        pub fn owner_qualification_pi_holder_count(tid: i32) -> i32;
        pub fn owner_qualification_watchdog_start(milliseconds: u32) -> i32;
        pub fn owner_qualification_watchdog_stop() -> i32;
    }
}

pub const NATIVE: bool = cfg!(target_os = "nuttx");

pub fn fifo_policy() -> Option<i32> {
    #[cfg(target_os = "nuttx")]
    {
        Some(unsafe { native::owner_qualification_fifo() })
    }
    #[cfg(not(target_os = "nuttx"))]
    {
        None
    }
}

pub fn schedule() -> Schedule {
    #[cfg(target_os = "nuttx")]
    {
        let mut values = [0; 3];
        // SAFETY: exactly three writable i32 fields, native layout stays in C.
        assert_eq!(
            unsafe { native::owner_qualification_schedule(values.as_mut_ptr()) },
            0
        );
        Schedule {
            tid: Some(values[0]),
            policy: Some(values[1]),
            priority: Some(values[2]),
        }
    }
    #[cfg(not(target_os = "nuttx"))]
    Schedule {
        tid: None,
        policy: None,
        priority: None,
    }
}

pub fn enter(role: &'static str, priority: i32) -> Participant {
    // Prewarm ordinary thread identity before any phase marker or endpoint use.
    let _ = std::thread::current();
    let before = schedule();
    #[cfg(target_os = "nuttx")]
    {
        // SAFETY: target header adapter selects FIFO, returning native status.
        assert_eq!(
            unsafe { native::owner_qualification_set_schedule(-1, priority) },
            0
        );
    }
    let start = schedule();
    #[cfg(target_os = "nuttx")]
    {
        assert_eq!(start.priority, Some(priority));
        assert_eq!(
            start.policy,
            Some(unsafe { native::owner_qualification_fifo() })
        );
    }
    Participant {
        role,
        requested_priority: priority,
        before,
        start,
        end: start,
        restored: start,
    }
}

pub fn leave(mut participant: Participant) -> Participant {
    participant.end = schedule();
    assert_eq!(participant.end.priority, participant.start.priority);
    assert_eq!(participant.end.policy, participant.start.policy);
    #[cfg(target_os = "nuttx")]
    {
        assert_eq!(
            unsafe {
                native::owner_qualification_set_schedule(
                    participant.before.policy.unwrap(),
                    participant.before.priority.unwrap(),
                )
            },
            0
        );
    }
    participant.restored = schedule();
    assert_eq!(participant.restored.priority, participant.before.priority);
    assert_eq!(participant.restored.policy, participant.before.policy);
    participant
}

pub fn waiting(tid: i32, priority: i32) -> bool {
    #[cfg(target_os = "nuttx")]
    {
        unsafe { native::owner_qualification_waiting(tid, priority) != 0 }
    }
    #[cfg(not(target_os = "nuttx"))]
    {
        let _ = (tid, priority);
        false
    }
}

pub fn mutex_configuration() -> Option<MutexConfiguration> {
    #[cfg(target_os = "nuttx")]
    {
        let mut values = [0; 6];
        // SAFETY: six writable scalar fields; pthread types stay in C.
        assert_eq!(
            unsafe { native::owner_qualification_mutex_configuration(values.as_mut_ptr()) },
            0
        );
        Some(MutexConfiguration {
            priority_inheritance: values[0] != 0,
            default_prio_inherit: values[1] != 0,
            default_protocol: values[2],
            protocol_none: values[3],
            protocol_inherit: values[4],
            holder_capacity: values[5],
        })
    }
    #[cfg(not(target_os = "nuttx"))]
    {
        None
    }
}

pub fn holder_state(tid: i32) -> Option<HolderState> {
    #[cfg(target_os = "nuttx")]
    {
        let state = thread_state(tid).unwrap();
        // SAFETY: target header readback returns a count or negative errno.
        // The coordinator keeps this task parked across both scalar reads.
        let count = unsafe { native::owner_qualification_pi_holder_count(tid) };
        assert!(count >= 0, "native PI holder observation failed");
        Some(HolderState {
            state,
            tracked_holders: count as usize,
        })
    }
    #[cfg(not(target_os = "nuttx"))]
    {
        let _ = tid;
        None
    }
}

pub fn thread_state(tid: i32) -> Option<ThreadState> {
    #[cfg(target_os = "nuttx")]
    {
        let mut values = [0; 6];
        // SAFETY: six writable scalar fields; no Rust interpretation of TCB layout.
        assert_eq!(
            unsafe { native::owner_qualification_thread_state(tid, values.as_mut_ptr()) },
            0
        );
        Some(ThreadState {
            tid,
            policy: values[0],
            effective_priority: values[1],
            base_priority: values[2],
            state: values[3],
            waiting_semaphore: values[4] != 0,
            runnable: values[5] != 0,
        })
    }
    #[cfg(not(target_os = "nuttx"))]
    {
        let _ = tid;
        None
    }
}

pub fn watchdog(start: bool) {
    #[cfg(target_os = "nuttx")]
    {
        let result = unsafe {
            if start {
                native::owner_qualification_watchdog_start(15_000)
            } else {
                native::owner_qualification_watchdog_stop()
            }
        };
        assert_eq!(result, 0);
    }
    #[cfg(not(target_os = "nuttx"))]
    let _ = start;
}
