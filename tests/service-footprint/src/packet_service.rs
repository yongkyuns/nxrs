//! Fixed-buffer service workload shared with the standalone CPU kernels.
use crate::payload_kernels::{nxrs_rust_encode, nxrs_rust_filter_xyz, nxrs_rust_parse};

pub fn token(lane: u8, producer: u8, sequence: u16) -> u32 {
    (u32::from(lane) << 24) | (u32::from(producer) << 16) | u32::from(sequence)
}

pub fn build(payload: &mut [u8; 236], lane: u8, producer: u8, sequence: u16) {
    let token = token(lane, producer, sequence);
    #[cfg(not(feature = "packet-inplace-samples"))]
    let samples: [i16; 96] = std::array::from_fn(|i| {
        ((token.wrapping_add(i as u32 * 7919) & 65535) as i32 - 32768) as i16
    });
    #[cfg(feature = "packet-inplace-samples")]
    let mut samples = [0i16; 96];
    #[cfg(feature = "packet-inplace-samples")]
    for (i, sample) in samples.iter_mut().enumerate() {
        *sample = ((token.wrapping_add(i as u32 * 7919) & 65535) as i32 - 32768) as i16;
    }
    payload[200..].fill(0);
    // SAFETY: disjoint initialized fixed-size buffers outlive the call.
    let status = unsafe { nxrs_rust_encode(payload.as_mut_ptr(), 200, token, samples.as_ptr()) };
    assert_eq!(status, 0);
}

pub fn process(payload: &[u8; 236], token: u32, state: &mut [i32; 3]) -> Result<[i32; 3], ()> {
    let mut samples = [0i16; 96];
    let mut sequence = 0;
    // SAFETY: disjoint initialized fixed buffers and exact validated length.
    let status =
        unsafe { nxrs_rust_parse(payload.as_ptr(), 200, samples.as_mut_ptr(), &mut sequence) };
    if status != 0 || sequence != token {
        return Err(());
    }
    let mut filtered = [0i32; 96];
    // SAFETY: state begins at zero and each update preserves signed-16 range;
    // every buffer is initialized and disjoint.
    unsafe { nxrs_rust_filter_xyz(samples.as_ptr(), state.as_mut_ptr(), filtered.as_mut_ptr()) };
    Ok(*state)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn successive_frames_match_wider_reference_and_reject_bad_packet() {
        let mut state = [0; 3];
        let mut expected = [0i64; 3];
        let mut payload = [0xff; 236];
        for sequence in 0..32 {
            build(&mut payload, 44, 3, sequence);
            assert!(payload[200..].iter().all(|&byte| byte == 0));
            let id = token(44, 3, sequence);
            for i in 0..96 {
                let sample = (id.wrapping_add(i * 7919) & 65535) as i64 - 32768;
                expected[(i % 3) as usize] += (sample - expected[(i % 3) as usize]) / 8;
            }
            assert_eq!(
                process(&payload, id, &mut state).unwrap(),
                expected.map(|x| x as i32)
            );
        }
        let before = state;
        payload[6] = 2;
        assert_eq!(process(&payload, token(44, 3, 31), &mut state), Err(()));
        assert_eq!(state, before);
    }
}
