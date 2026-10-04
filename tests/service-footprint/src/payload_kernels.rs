const SAMPLES: usize = 96;
const PACKET_BYTES: usize = 200;

fn encode(packet: &mut [u8; PACKET_BYTES], sequence: u32, samples: &[i16; SAMPLES]) {
    packet[..4].copy_from_slice(&sequence.to_le_bytes());
    packet[4..8].copy_from_slice(&[32, 0, 1, 0]);
    for (bytes, sample) in packet[8..].chunks_exact_mut(2).zip(samples) {
        bytes.copy_from_slice(&sample.to_le_bytes());
    }
}

fn parse(packet: &[u8], samples: &mut [i16; SAMPLES], sequence: &mut u32) -> bool {
    if packet.len() != PACKET_BYTES || packet[4..8] != [32, 0, 1, 0] {
        return false;
    }
    *sequence = u32::from_le_bytes([packet[0], packet[1], packet[2], packet[3]]);
    for (bytes, sample) in packet[8..].chunks_exact(2).zip(samples) {
        *sample = i16::from_le_bytes([bytes[0], bytes[1]]);
    }
    true
}

// The FFI contract keeps each state component in the signed 16-bit range.
// A step toward a signed-16 sample preserves this invariant and cannot overflow.
fn filter(samples: &[i16; SAMPLES], state: &mut [i32; 3], output: &mut [i32; SAMPLES]) {
    for (index, (sample, result)) in samples.iter().zip(output).enumerate() {
        let axis = index % 3;
        state[axis] += (i32::from(*sample) - state[axis]) / 8;
        *result = state[axis];
    }
}

fn filter_xyz(samples: &[i16; SAMPLES], state: &mut [i32; 3], output: &mut [i32; SAMPLES]) {
    for (frame, filtered) in samples.chunks_exact(3).zip(output.chunks_exact_mut(3)) {
        for ((sample, result), component) in frame.iter().zip(filtered).zip(state.iter_mut()) {
            *component += (i32::from(*sample) - *component) / 8;
            *result = *component;
        }
    }
}

#[unsafe(no_mangle)]
#[inline(never)]
/// # Safety
/// Invalid lengths or null arguments return -1 without accessing buffers.
/// The storage requirements below apply only to valid-length, non-null calls.
/// Non-null pointers must name live, aligned, disjoint buffers: packet has
/// `length` initialized writable bytes and samples has 96 readable i16 values.
pub unsafe extern "C" fn nxrs_rust_encode(
    packet: *mut u8,
    length: usize,
    sequence: u32,
    samples: *const i16,
) -> i32 {
    if length != PACKET_BYTES || packet.is_null() || samples.is_null() {
        return -1;
    }
    // SAFETY: validated length and caller's fixed-buffer/aliasing contract.
    encode(
        unsafe { &mut *packet.cast::<[u8; PACKET_BYTES]>() },
        sequence,
        unsafe { &*samples.cast::<[i16; SAMPLES]>() },
    );
    0
}

#[unsafe(no_mangle)]
#[inline(never)]
/// # Safety
/// Invalid lengths or null arguments return -1 without accessing buffers.
/// The storage requirements below apply only to valid-length, non-null calls.
/// Non-null pointers must name live, aligned, disjoint buffers: packet has
/// `length` readable bytes, samples 96 initialized writable i16 values, sequence
/// one initialized writable u32.
pub unsafe extern "C" fn nxrs_rust_parse(
    packet: *const u8,
    length: usize,
    samples: *mut i16,
    sequence: *mut u32,
) -> i32 {
    if length != PACKET_BYTES || packet.is_null() || samples.is_null() || sequence.is_null() {
        return -1;
    }
    // SAFETY: caller guarantees valid non-overlapping storage with these sizes.
    -i32::from(!parse(
        unsafe { &*packet.cast::<[u8; PACKET_BYTES]>() },
        unsafe { &mut *samples.cast::<[i16; SAMPLES]>() },
        unsafe { &mut *sequence },
    ))
}

#[unsafe(no_mangle)]
#[inline(never)]
/// # Safety
/// Pointers name disjoint, aligned buffers containing 96 readable i16 samples,
/// three writable i32 states in [-32768,32767], and 96 initialized writable i32 outputs.
pub unsafe extern "C" fn nxrs_rust_filter(samples: *const i16, state: *mut i32, output: *mut i32) {
    // SAFETY: fixed-size valid storage and initial state invariant are supplied
    // by the C runner; all references end before this synchronous call returns.
    filter(
        unsafe { &*samples.cast::<[i16; SAMPLES]>() },
        unsafe { &mut *state.cast::<[i32; 3]>() },
        unsafe { &mut *output.cast::<[i32; SAMPLES]>() },
    );
}

#[unsafe(no_mangle)]
#[inline(never)]
/// # Safety
/// Same fixed-buffer, disjointness, and state-range contract as nxrs_rust_filter.
pub unsafe extern "C" fn nxrs_rust_filter_xyz(
    samples: *const i16,
    state: *mut i32,
    output: *mut i32,
) {
    // SAFETY: caller supplies valid buffers and states in the signed-16 range.
    filter_xyz(
        unsafe { &*samples.cast::<[i16; SAMPLES]>() },
        unsafe { &mut *state.cast::<[i32; 3]>() },
        unsafe { &mut *output.cast::<[i32; SAMPLES]>() },
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signed_endian_round_trip_overwrites_all_output() {
        for sequence in [0, 0x1234_5678, u32::MAX] {
            let samples = std::array::from_fn(|i| [i16::MIN, -1, 0, 1, i16::MAX][i % 5]);
            let mut packet = [0xa5; PACKET_BYTES];
            encode(&mut packet, sequence, &samples);
            assert_eq!(&packet[..4], &sequence.to_le_bytes());
            assert_eq!(&packet[4..12], &[32, 0, 1, 0, 0, 128, 255, 255]);
            let mut decoded = [17; SAMPLES];
            let mut decoded_sequence = !sequence;
            assert!(parse(&packet, &mut decoded, &mut decoded_sequence));
            assert_eq!(decoded_sequence, sequence);
            assert_eq!(decoded, samples);
        }
    }

    #[test]
    fn malformed_packets_leave_outputs_unchanged() {
        let mut packet = [0; PACKET_BYTES];
        encode(&mut packet, 7, &[0; SAMPLES]);
        for length in [0, 7, 199, 201] {
            let data = vec![0; length];
            let mut samples = [42; SAMPLES];
            let mut sequence = 99;
            assert!(!parse(&data, &mut samples, &mut sequence));
            assert_eq!(samples, [42; SAMPLES]);
            assert_eq!(sequence, 99);
        }
        for index in 4..8 {
            let mut bad = packet;
            bad[index] ^= 1;
            let mut samples = [42; SAMPLES];
            let mut sequence = 99;
            assert!(!parse(&bad, &mut samples, &mut sequence));
            assert_eq!(samples, [42; SAMPLES]);
            assert_eq!(sequence, 99);
        }
    }

    #[test]
    fn filter_truncates_toward_zero_and_keeps_axes_independent() {
        let samples = std::array::from_fn(|i| [i16::MIN, i16::MAX, -7][i % 3]);
        let mut state = [32767, -32768, 0];
        let mut expected = state;
        let mut output = [0; SAMPLES];
        filter(&samples, &mut state, &mut output);
        for i in 0..SAMPLES {
            let axis = i % 3;
            // Wider independent reference checks the fixed-width arithmetic.
            let step = (i64::from(samples[i]) - i64::from(expected[axis])) / 8;
            expected[axis] = (i64::from(expected[axis]) + step) as i32;
            assert_eq!(output[i], expected[axis]);
            assert!((i32::from(i16::MIN)..=i32::from(i16::MAX)).contains(&output[i]));
        }
        assert_eq!(state, expected);
        let mut grouped_state = [32767, -32768, 0];
        let mut grouped_output = [0; SAMPLES];
        filter_xyz(&samples, &mut grouped_state, &mut grouped_output);
        assert_eq!(grouped_state, state);
        assert_eq!(grouped_output, output);
    }

    #[test]
    fn ffi_rejects_absent_buffers_before_dereferencing() {
        // SAFETY: invalid size/null cases return before accessing any storage.
        unsafe {
            assert_eq!(
                nxrs_rust_encode(std::ptr::null_mut(), 200, 0, std::ptr::null()),
                -1
            );
            assert_eq!(
                nxrs_rust_parse(
                    std::ptr::null(),
                    200,
                    std::ptr::null_mut(),
                    std::ptr::null_mut()
                ),
                -1
            );
        }
    }
}
