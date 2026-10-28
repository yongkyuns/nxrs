//! Validated sampling observations. Missing taps are unknown, never zero loss.
use crate::{header, line, replay, Result};
use csi_lab_core::{journal, sampling};
use serde_json::json;
use std::{fs::File, io::BufReader};

pub fn run(path: &str) -> Result<()> {
    // No useful-looking prefix from a corrupt or incomplete recording.
    replay(path, None, false)?;
    let mut reader = BufReader::new(File::open(path)?);
    let _ = header(&mut reader)?;
    let (mut replies, mut timeouts, mut failed_queries) = (0u64, 0u64, 0u64);
    let mut windows = [0u64; 4];
    let mut max_ping_wait_ms = None::<u32>;
    while let Some(line) = line(&mut reader)? {
        let record = journal::Record::parse(&line)?;
        if record.kind != journal::SAMPLING {
            continue;
        }
        sampling::validate(&record)?;
        let w = record.data();
        let value = if w[1] == 1 {
            // The callback outcome is observed even when profile extraction
            // fails. Its duration and aggregate SDK counters remain unknown.
            if w[2] == 1 {
                replies += 1;
            } else {
                timeouts += 1;
            }
            if w[4] == 0 {
                max_ping_wait_ms = Some(max_ping_wait_ms.unwrap_or(0).max(w[5]));
            } else {
                failed_queries += 1;
            }
            json!({"event":"ping_callback","time_us":record.time_us,
                "outcome":if w[2]==1 {"reply"} else {"timeout"},
                "sequence":w[3],"profile_available":w[4]==0,
                "profile_status":w[4] as i32,"task_min_free_stack_bytes":w[8],
                "elapsed_ms":if w[4]==0 {Some(w[5])} else {None},
                "sent":if w[4]==0 {Some(w[6])} else {None},
                "received":if w[4]==0 {Some(w[7])} else {None}})
        } else {
            windows[w[2] as usize - 1] += 1;
            json!({"event":"sdk_window","time_us":record.time_us,
                "outcome":(["timestamp_reset","sparse_flush","accepted","accumulating"][w[2] as usize-1]),
                "trigger_source_sequence":w[3],"radio_timestamp_ms":w[4],"rows":w[5],
                "span_ms":w[6] as i32,"delta_ms":w[7] as i32,
                "delta_limit_ms":w[8],"handle_window":w[9],"handle_time_ms":w[10],"task_min_free_stack_bytes":w[11]})
        };
        println!("{value}");
    }
    println!(
        "{}",
        json!({"event":"summary","original_capture_verified":true,
        "ping_observed":replies+timeouts>0,
        "ping_reply_callbacks":replies,"ping_timeout_callbacks":timeouts,
        "ping_profile_query_failures":failed_queries,"max_ping_wait_ms":max_ping_wait_ms,
        "sdk_window_observed":windows.iter().sum::<u64>()>0,
        "window_counts_timestamp_reset_sparse_flush_accepted_accumulating":windows,
        "sampling_cause_automatically_inferred":false,"physical_detector_admitted":false,
        "scope":"SDK callback completion and window decisions; window values are SDK milliseconds (RX stamp is original u32 microseconds divided by 1000), not actual ICMP send timestamps, complete PHY metadata, accuracy or CPU cost."})
    );
    Ok(())
}
