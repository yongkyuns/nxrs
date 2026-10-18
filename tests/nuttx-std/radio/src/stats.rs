//! Session-relative native counters, not radio delivery acknowledgements.
#[cfg(target_os = "nuttx")]
use std::ffi::{c_char, CString};
#[cfg(target_os = "nuttx")]
use std::net::SocketAddrV4;

#[cfg(target_os = "nuttx")]
unsafe extern "C" {
    fn nxrs_radio_net_snapshot(interface: *const c_char, values: *mut u32, count: usize) -> i32;
    fn nxrs_radio_interface_snapshot(
        interface: *const c_char,
        values: *mut u32,
        count: usize,
    ) -> i32;
    fn nxrs_radio_trace_start(interface: *const c_char, peer: *const u8, port: u16) -> i32;
    fn nxrs_radio_trace_stop();
    fn nxrs_radio_trace_snapshot(sequence: u32, values: *mut u32, count: usize) -> i32;
    fn nxrs_radio_rx_snapshot(values: *mut u32, count: usize) -> i32;
    fn nxrs_radio_daemon_snapshot(values: *mut u32, count: usize) -> i32;
    fn nxrs_radio_controller_snapshot(
        interface: *const c_char,
        values: *mut u32,
        count: usize,
    ) -> i32;
}

#[cfg(target_os = "nuttx")]
pub(super) struct Counters {
    counters: Option<[u32; 12]>,
    interface: Option<[u32; 3]>,
    observer: bool,
    trace: bool,
    controller: Option<[u32; 30]>,
    receive: Option<[u32; 16]>,
    daemon: Option<[u32; 19]>,
    controller_reported: bool,
}

#[cfg(target_os = "nuttx")]
fn capture() -> Option<[u32; 12]> {
    // Select the same interface as the existing Wi-Fi provider.
    let interface = CString::new(option_env!("NXRS_WIFI_INTERFACE").unwrap_or("wlan0")).ok()?;
    let mut values = [0; 12];
    // SAFETY: fixed-width output array and input string live for this call only.
    let status =
        unsafe { nxrs_radio_net_snapshot(interface.as_ptr(), values.as_mut_ptr(), values.len()) };
    (status == 0 && (1..=32).contains(&values[0])).then_some(values)
}

#[cfg(target_os = "nuttx")]
fn capture_interface() -> Option<[u32; 3]> {
    let interface = CString::new(option_env!("NXRS_WIFI_INTERFACE").unwrap_or("wlan0")).ok()?;
    let mut values = [0; 3];
    // SAFETY: fixed-width output array and input string live for this call only.
    let status = unsafe {
        nxrs_radio_interface_snapshot(interface.as_ptr(), values.as_mut_ptr(), values.len())
    };
    (status == 0).then_some(values)
}

#[cfg(target_os = "nuttx")]
fn capture_controller() -> Option<[u32; 30]> {
    let interface = CString::new(option_env!("NXRS_WIFI_INTERFACE").unwrap_or("wlan0")).ok()?;
    let mut values = [0; 30];
    // SAFETY: the driver owns its native wire layouts. This bridge only
    // exchanges fixed-width values and retains no pointers.
    let status = unsafe {
        nxrs_radio_controller_snapshot(interface.as_ptr(), values.as_mut_ptr(), values.len())
    };
    (status == 0).then_some(values)
}

fn controller_delta(before: [u32; 30], after: [u32; 30]) -> ([u32; 18], bool, bool) {
    let packets = before[0] == 0 && after[0] == 0;
    let retries =
        before[1] == 0 && after[1] == 0 && before[2..4] == after[2..4] && before[21] == after[21];
    let values = std::array::from_fn(|i| {
        if (i < 5 && packets) || (i >= 5 && retries) {
            after[i + 4].wrapping_sub(before[i + 4])
        } else {
            0
        }
    });
    (values, packets, retries)
}

fn phy_delta(before: [u32; 30], after: [u32; 30]) -> Option<[u32; 7]> {
    let (_, _, valid) = controller_delta(before, after);
    (valid && before[22] == 1 && after[22] == 1)
        .then(|| std::array::from_fn(|i| after[23 + i].wrapping_sub(before[23 + i])))
}

fn tick_age_ms(now: u32, then: u32, nanoseconds_per_tick: u32) -> u32 {
    ((u64::from(now.wrapping_sub(then)) * u64::from(nanoseconds_per_tick)) / 1_000_000)
        .min(u64::from(u32::MAX)) as u32
}

#[cfg(target_os = "nuttx")]
fn capture_daemon() -> Option<[u32; 19]> {
    let mut values = [0; 19];
    // SAFETY: fixed-width copy under the fixture lock, no bus access or waits.
    let status = unsafe { nxrs_radio_daemon_snapshot(values.as_mut_ptr(), values.len()) };
    (status == 0 && values[1] != 0).then_some(values)
}

#[cfg(target_os = "nuttx")]
fn capture_receive() -> Option<[u32; 16]> {
    let mut values = [0; 16];
    // SAFETY: the C fixture copies bounded numeric counts under its lock.
    let status = unsafe { nxrs_radio_rx_snapshot(values.as_mut_ptr(), values.len()) };
    (status == 0).then_some(values)
}

fn interface_comparison(
    before: Option<[u32; 3]>,
    after: Option<[u32; 3]>,
) -> Option<(bool, bool, bool, bool)> {
    let (before, after) = (before?, after?);
    Some((
        after[0] != 0,
        after[1] != 0,
        after[2] != 0,
        before[2] == after[2],
    ))
}

#[cfg(target_os = "nuttx")]
impl Counters {
    pub(super) fn start(peer: SocketAddrV4, datagram: bool) -> Self {
        // Bus/daemon events apply to TCP too. Only per-packet sequence tracing
        // is UDP-specific; keep observer lifetime independent of that filter.
        let observer = CString::new(option_env!("NXRS_WIFI_INTERFACE").unwrap_or("wlan0"))
            .is_ok_and(|interface| {
                // SAFETY: the C fixture copies the peer octets and retains
                // no Rust pointers. The observer owns bounded static storage.
                unsafe {
                    nxrs_radio_trace_start(
                        interface.as_ptr(),
                        peer.ip().octets().as_ptr(),
                        peer.port(),
                    ) == 0
                }
            });
        // Exclude our baseline controller queries from receive-path deltas.
        let controller = capture_controller();
        let receive = capture_receive();
        let daemon = capture_daemon();
        Self {
            counters: capture(),
            interface: capture_interface(),
            observer,
            trace: datagram && observer,
            controller,
            receive,
            daemon,
            controller_reported: false,
        }
    }

    pub(super) fn report_trace(&self, sequence: u32, stage: &str) {
        if stage != "previous" {
            self.report_daemon(stage);
        }
        let mut values = [0; 25];
        // SAFETY: a fixed-width writable array, retained only for this call.
        if !self.trace
            || unsafe { nxrs_radio_trace_snapshot(sequence, values.as_mut_ptr(), values.len()) }
                != 0
        {
            println!("NXRS_RADIO_PACKET_TRACE stage={stage} sequence={sequence} available=0");
            return;
        }
        println!(
            "NXRS_RADIO_PACKET_TRACE stage={stage} sequence={sequence} available=1 record_valid={} net_tx={} bus_tx={} bus_result={} bus_rx={} net_rx={} total_net_tx={} total_bus_tx={} total_bus_rx={} total_net_rx={} evicted_records={}",
            values[0], u32::from(values[1] & 1 != 0), u32::from(values[1] & 2 != 0), values[2] as i32,
            u32::from(values[1] & 4 != 0), u32::from(values[1] & 8 != 0), values[3], values[4], values[5], values[6], values[7],
        );
        println!(
            "NXRS_RADIO_PACKET_HEADERS stage={stage} sequence={sequence} ip_checksum_bad_stages={} udp_checksum_bad_stages={} udp_checksum_omitted_stages={} local_ip_bad_stages={} local_port_changed_stages={} bus_known={} bus_sequence={} bus_credit={} bus_cached_ready={}",
            values[8], values[9], values[10], values[11], values[12], values[13], values[14], values[15], values[16],
        );
        println!(
            "NXRS_RADIO_BUS_STATUS stage={stage} sequence={sequence} post_known={} post_read_result={} ready={} data_missing={} underflow={} overflow={} command_error={} packet_available={}",
            values[17], values[18] as i32, values[19], values[20], values[21], values[22], values[23], values[24],
        );
    }

    pub(super) fn report_controller(&mut self, stage: &str) {
        self.controller_reported = true;
        self.report_daemon(stage);
        // Snapshot before the final query adds control-channel traffic.
        if let Some((before, after)) = self.receive.zip(capture_receive()) {
            let values: [u32; 16] = std::array::from_fn(|i| after[i].wrapping_sub(before[i]));
            println!(
                "NXRS_RADIO_RX_PATH stage={stage} available=1 attempt={} short={} no_buffer={} oversize={} spi_error={} empty={} header_error={} offset_error={} control={} event={} data={} unknown={} notify={} worker={} coalesced={} queue_error={}",
                values[0], values[1], values[2], values[3], values[4], values[5], values[6], values[7],
                values[8], values[9], values[10], values[11], values[12], values[13], values[14], values[15],
            );
        } else {
            println!("NXRS_RADIO_RX_PATH stage={stage} available=0");
        }
        let Some((before, after)) = self.controller.zip(capture_controller()) else {
            println!("NXRS_RADIO_MAC_STATS stage={stage} available=0");
            return;
        };
        let (values, packets, retries) = controller_delta(before, after);
        println!(
            "NXRS_RADIO_MAC_STATS stage={stage} available=1 packets_valid={} retries_valid={} baseline_packet_result={} packet_result={} baseline_retry_result={} retry_result={} counter_version={} counter_length={} rx_good_pkt={} rx_bad_pkt={} tx_good_pkt={} tx_bad_pkt={} rx_ocast_good_pkt={} txframe={} txretrans={} txretry={} txretrie={} txfail={}",
            u32::from(packets), u32::from(retries), before[0] as i32, after[0] as i32, before[1] as i32, after[1] as i32,
            after[2], after[3], values[0], values[1], values[2], values[3], values[4], values[5], values[6], values[7], values[8], values[9],
        );
        let layout_valid = before[1] == 0 && after[1] == 0 && before[2..4] == after[2..4];
        println!(
            "NXRS_RADIO_MAC_RX stage={stage} available=1 counters_valid={} reset_known={} reset_delta={} rxframe={} rxerror={} rxnobuf={} rxoflo={} rxfilter={} rxbadproto={} rxfragerr={}",
            u32::from(retries), u32::from(layout_valid),
            if layout_valid { after[21].wrapping_sub(before[21]) } else { 0 },
            values[10], values[11], values[12], values[13], values[14], values[15], values[16],
        );
        if let Some(phy) = phy_delta(before, after) {
            println!(
                "NXRS_RADIO_MAC_PHY stage={stage} available=1 rxbadfcs={} rxbadplcp={} rxcrsglitch={} rxfrmtoolong={} rxfrmtooshrt={} rxinvmachdr={} rxstrt={}",
                phy[0], phy[1], phy[2], phy[3], phy[4], phy[5], phy[6],
            );
        } else {
            println!("NXRS_RADIO_MAC_PHY stage={stage} available=0");
        }
    }

    fn report_daemon(&self, stage: &str) {
        let Some((before, after)) = self.daemon.zip(capture_daemon()) else {
            println!("NXRS_RADIO_DAEMON stage={stage} available=0");
            return;
        };
        let delta = |index: usize| after[index].wrapping_sub(before[index]);
        let age = |index: usize| tick_age_ms(after[0], after[index], after[1]);
        let flags = after[10];
        println!(
            "NXRS_RADIO_DAEMON stage={stage} available=1 irq={} irq_known={} irq_age_ms={} polls={} read_errors={} poll_known={} poll_age_ms={} poll_result={} packet_available={} packet_length={} fifo_underflow={} fifo_overflow={} command_error={} wait_known={} waiting={} waits={} wakes={} wake_known={} wake_age_ms={} available_reads={}",
            delta(2), after[3], if after[3] != 0 { age(4) } else { 0 },
            delta(5), delta(6), after[7], if after[7] != 0 { age(8) } else { 0 }, after[9] as i32,
            u32::from(flags & 32 != 0), after[11], u32::from(flags & 4 != 0), u32::from(flags & 8 != 0),
            u32::from(flags & 16 != 0), after[12], after[13], delta(14), delta(15),
            after[16], if after[16] != 0 { age(17) } else { 0 }, delta(18),
        );
    }

    pub(super) fn report(&self, stage: &str) {
        let Some((before, after)) = self.counters.zip(capture()) else {
            println!("NXRS_RADIO_NET_STATS stage={stage} available=0");
            return;
        };
        let values = delta(before, after);
        println!(
            "NXRS_RADIO_NET_STATS stage={stage} udp_sent={} udp_recv={} udp_drop={} udp_checksum={} tcp_retransmit={} tcp_drop={} tcp_checksum={} ipv4_drop={} driver_tx={} driver_rx_ipv4={} driver_rx_unsupported={}",
            values[1], values[2], values[3], values[4], values[5], values[6],
            values[7], values[8], values[9], values[10], values[11],
        );
    }

    pub(super) fn report_interface(&self, stage: &str) {
        let current = capture_interface();
        let Some((up, carrier, ipv4_assigned, ipv4_unchanged)) =
            interface_comparison(self.interface, current)
        else {
            println!("NXRS_RADIO_INTERFACE stage={stage} available=0");
            return;
        };
        // Show what the driver reported before traffic too; a cached carrier
        // bit that was already clear is not evidence of a new association loss.
        if let Some(baseline) = self.interface {
            println!(
                "NXRS_RADIO_INTERFACE_BASELINE stage={stage} up={} carrier={} ipv4_assigned={}",
                u32::from(baseline[0] != 0),
                u32::from(baseline[1] != 0),
                u32::from(baseline[2] != 0)
            );
        }
        println!(
            "NXRS_RADIO_INTERFACE stage={stage} available=1 up={} carrier={} ipv4_assigned={} ipv4_unchanged={}",
            u32::from(up), u32::from(carrier), u32::from(ipv4_assigned), u32::from(ipv4_unchanged)
        );
    }
}

#[cfg(target_os = "nuttx")]
impl Drop for Counters {
    fn drop(&mut self) {
        // Report errors too, before Wi-Fi is disconnected by the outer helper.
        self.report("final");
        if !self.controller_reported {
            self.report_controller("final");
        }
        if self.observer {
            // SAFETY: disables only the fixture's synchronous read-only tap.
            unsafe { nxrs_radio_trace_stop() };
        }
    }
}

fn delta(before: [u32; 12], after: [u32; 12]) -> [u32; 12] {
    let mask = u32::MAX >> (32 - before[0]);
    std::array::from_fn(|index| {
        let value = after[index].wrapping_sub(before[index]);
        if (1..=8).contains(&index) {
            value & mask
        } else {
            value
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn controller_deltas_require_valid_queries_and_identical_layouts() {
        let mut before = [0; 30];
        before[2] = 6;
        before[3] = 1000;
        before[6] = u32::MAX;
        let mut after = before;
        after[6] = 2;
        after[11] = 10;
        let (values, packets, retries) = controller_delta(before, after);
        assert!(packets && retries);
        assert_eq!(values[2], 3);
        assert_eq!(values[7], 10);
        after[2] = 7;
        let (values, packets, retries) = controller_delta(before, after);
        assert!(packets && !retries);
        assert_eq!(values[7], 0);
        after[0] = (-5i32) as u32;
        assert_eq!(controller_delta(before, after), ([0; 18], false, false));
    }

    #[test]
    fn receive_counts_share_layout_validation_and_reject_controller_reset() {
        let mut before = [0; 30];
        before[2] = 30;
        before[3] = 224;
        before[14] = u32::MAX;
        let mut after = before;
        after[14] = 2;
        after[16] = 3;
        let (values, _, valid) = controller_delta(before, after);
        assert!(valid);
        assert_eq!(values[10], 3);
        assert_eq!(values[12], 3);
        after[21] += 1;
        let (values, packets, valid) = controller_delta(before, after);
        assert!(packets && !valid);
        assert!(values[5..].iter().all(|value| *value == 0));
    }

    #[test]
    fn phy_deltas_reject_unavailable_layouts_failed_queries_and_resets() {
        let mut before = [0; 30];
        before[2] = 10;
        before[3] = 848;
        before[22] = 1;
        before[23] = u32::MAX;
        let mut after = before;
        after[23] = 2;
        assert_eq!(phy_delta(before, after).unwrap()[0], 3);
        after[22] = 0;
        assert!(phy_delta(before, after).is_none());
        after[22] = 1;
        after[21] += 1;
        assert!(phy_delta(before, after).is_none());
        after[21] = before[21];
        after[1] = (-5i32) as u32;
        assert!(phy_delta(before, after).is_none());
    }

    #[test]
    fn daemon_ages_support_tick_wrap_and_submillisecond_resolution() {
        assert_eq!(tick_age_ms(2, u32::MAX, 10_000_000), 30);
        assert_eq!(tick_age_ms(12, 2, 100_000), 1);
        assert_eq!(tick_age_ms(u32::MAX, 0, u32::MAX), u32::MAX);
    }

    #[test]
    fn native_protocol_wrap_and_driver_wrap_have_distinct_widths() {
        let mut before = [0; 12];
        before[0] = 16;
        before[1] = 65534;
        before[5] = 65535;
        before[9] = u32::MAX;
        let mut after = before;
        after[1] = 1;
        after[5] = 0;
        after[9] = 2;
        let values = delta(before, after);
        assert_eq!(values[1], 3);
        assert_eq!(values[5], 1);
        assert_eq!(values[9], 3);
    }

    #[test]
    fn snapshots_are_relative_not_cumulative_totals() {
        let mut before = [45; 12];
        before[0] = 32;
        let mut after = before;
        after[2] += 100;
        let values = delta(before, after);
        assert_eq!(values[2], 100);
        assert_eq!(values[1], 0);
        assert_eq!(values[5], 0);
    }

    #[test]
    fn interface_comparison_reports_cached_flags_and_address_change_only_as_booleans() {
        assert_eq!(
            interface_comparison(Some([1, 1, 0x0102_0304]), Some([0, 1, 0x0506_0708])),
            Some((false, true, true, false))
        );
        assert_eq!(
            interface_comparison(Some([0, 0, 0]), Some([1, 0, 0])),
            Some((true, false, false, true))
        );
        assert_eq!(interface_comparison(None, Some([1, 1, 1])), None);
        assert_eq!(interface_comparison(Some([1, 1, 1]), None), None);
    }
}
