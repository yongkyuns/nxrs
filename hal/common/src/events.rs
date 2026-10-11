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
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RequestId {
    pub session: SessionId,
    pub operation: OperationId,
}

/// Rejection transfers the original owned value back to the producer.
#[derive(Debug, PartialEq, Eq)]
pub struct Rejected<T, E> {
    pub value: T,
    pub error: E,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DeliveryError {
    Full,
    Closed,
}

/// Producer-side admission only: never call a receiving service inline.
/// Providers retain/retry important rejected events with bounded storage, or
/// expose a sticky terminal failure; they must not silently discard them.
pub trait EventSink<E> {
    fn try_emit(&mut self, event: E) -> Result<(), Rejected<E, DeliveryError>>;
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
    fn payload_length_is_not_capacity() {
        let bytes = BoundedBytes::<4>::new(&[1, 2]).unwrap();
        assert_eq!(bytes.as_slice(), &[1, 2]);
        assert!(BoundedBytes::<4>::new(&[0; 5]).is_none());
        assert_eq!(BoundedBytes::<0>::new(&[]).unwrap().as_slice(), &[]);
    }
}
