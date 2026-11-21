use core::num::NonZeroU64;

/// Identity of one provider lifetime. Owners must not reuse it across restarts.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SessionId(NonZeroU64);

impl SessionId {
    pub const fn new(value: u64) -> Option<Self> {
        match NonZeroU64::new(value) {
            Some(value) => Some(Self(value)),
            None => None,
        }
    }

    pub const fn get(self) -> u64 {
        self.0.get()
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct OperationId(NonZeroU64);

impl OperationId {
    pub const fn new(value: u64) -> Option<Self> {
        match NonZeroU64::new(value) {
            Some(value) => Some(Self(value)),
            None => None,
        }
    }

    pub const fn get(self) -> u64 {
        self.0.get()
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RequestId {
    pub session: SessionId,
    pub operation: OperationId,
}

/// Inline, bounded payload. Capacity is storage, not the current data length.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BoundedBytes<const N: usize> {
    bytes: [u8; N],
    // Do not add host-pointer-sized padding to small radio payloads.
    len: u32,
}

impl<const N: usize> BoundedBytes<N> {
    pub fn new(bytes: &[u8]) -> Option<Self> {
        if bytes.len() > N {
            return None;
        }
        let len = u32::try_from(bytes.len()).ok()?;
        let mut result = Self { bytes: [0; N], len };
        result.bytes[..bytes.len()].copy_from_slice(bytes);
        Some(result)
    }

    pub fn as_slice(&self) -> &[u8] {
        &self.bytes[..self.len as usize]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identities_reject_zero() {
        assert_eq!(SessionId::new(0), None);
        assert_eq!(OperationId::new(0), None);
    }

    #[test]
    fn identity_values_roundtrip() {
        for value in [1, 42, u64::MAX] {
            assert_eq!(SessionId::new(value).unwrap().get(), value);
            assert_eq!(OperationId::new(value).unwrap().get(), value);
        }
    }

    #[test]
    fn payload_length_is_not_capacity() {
        let bytes = BoundedBytes::<4>::new(&[1, 2]).unwrap();
        assert_eq!(bytes.as_slice(), &[1, 2]);
        assert!(BoundedBytes::<4>::new(&[0; 5]).is_none());
        assert_eq!(BoundedBytes::<0>::new(&[]).unwrap().as_slice(), &[]);
    }
}
