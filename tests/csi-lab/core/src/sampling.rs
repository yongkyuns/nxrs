//! Opt-in laboratory sampling provenance; never detector input or admission.
//! Values come from the public ping profile and the actual SDK window branch.
use crate::journal::{Record, SAMPLING};

pub const WORDS: usize = 12;
pub fn validate(record: &Record) -> Result<(), &'static str> {
    let w = record.data();
    if record.kind != SAMPLING || w.len() != WORDS || w[0] != 1 {
        return Err("sampling schema");
    }
    match w[1] {
        // Reply/timeout callback. A failed SDK profile query is retained as
        // missing diagnostics, rather than interpreted as valid zero counters.
        1 if (1..=2).contains(&w[2])
            && w[3] <= u16::MAX as u32
            && w[9..] == [0; 3]
            && (w[4] != 0 || w[7] <= w[6]) =>
        {
            Ok(())
        }
        // Window outcome: timestamp reset, sparse flush, accepted, accumulating.
        // Signed deltas deliberately retain rollback/invalid-time observations.
        2 if (1..=4).contains(&w[2]) && w[8] > 0 && w[9] > 0 && w[10] > 0 => Ok(()),
        _ => Err("sampling fields"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn check(w: &[u32]) -> bool {
        validate(&Record::new(SAMPLING, 1, w).unwrap()).is_ok()
    }
    #[test]
    fn missing_profile_queries_are_distinct_from_valid_zero_values() {
        let mut w = [1, 1, 2, 41, 0, 1000, 41, 40, 0, 0, 0, 0];
        assert!(check(&w));
        w[7] = 42;
        assert!(!check(&w));
        w[4] = 0x102; // Failed SDK query: do not reason about counters.
        assert!(check(&w));
        w[9] = 1;
        assert!(!check(&w));
        w[9] = 0;
        w[3] = 65536;
        assert!(!check(&w));
        assert!(!check(&w[..11]));
    }
    #[test]
    fn first_failed_native_send_can_timeout_with_zero_successful_transmissions() {
        // Pinned esp_ping_send increments sequence before sendto, but counts
        // transmission only on success. A failed first send still waits for
        // receive and invokes the timeout callback with valid zero counters.
        let mut w = [1, 1, 2, 1, 0, 1000, 0, 0, 1024, 0, 0, 0];
        assert!(check(&w));
        w[7] = 1;
        assert!(!check(&w));
    }
    #[test]
    fn all_window_outcomes_preserve_signed_native_deltas() {
        let mut w = [1, 2, 1, 100, 200, 0, 0, u32::MAX, 25000, 20, 50000, 0];
        for reason in 1..=4 {
            w[2] = reason;
            assert!(check(&w));
        }
        w[8] = 0;
        assert!(!check(&w));
        w[8] = 25000;
        w[2] = 5;
        assert!(!check(&w));
    }
}
