//! Fixed-capacity UTF-8 formatting for wire frames and display labels.
//!
//! Storage is inline. Formatting never allocates, truncates, or silently drops
//! text: callers receive [`core::fmt::Error`] when the supplied capacity is full.
//! A failed individual `write_str` leaves the previous bytes intact. A larger
//! formatting operation can have written earlier fragments before it fails.
#![no_std]
#![forbid(unsafe_code)]

use core::fmt;

/// An inline UTF-8 buffer whose capacity is expressed in bytes.
pub struct Text<const N: usize> {
    bytes: [u8; N],
    len: usize,
}

impl<const N: usize> Text<N> {
    /// Construct an empty buffer without allocating.
    pub const fn new() -> Self {
        Self {
            bytes: [0; N],
            len: 0,
        }
    }

    /// Return the initialized, formatted bytes.
    pub fn bytes(&self) -> &[u8] {
        &self.bytes[..self.len]
    }
}

impl<const N: usize> Default for Text<N> {
    fn default() -> Self {
        Self::new()
    }
}

impl<const N: usize> fmt::Write for Text<N> {
    fn write_str(&mut self, text: &str) -> fmt::Result {
        let end = self.len.checked_add(text.len()).ok_or(fmt::Error)?;
        self.bytes
            .get_mut(self.len..end)
            .ok_or(fmt::Error)?
            .copy_from_slice(text.as_bytes());
        self.len = end;
        Ok(())
    }
}

impl<const N: usize> AsRef<[u8]> for Text<N> {
    fn as_ref(&self) -> &[u8] {
        self.bytes()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::fmt::Write;

    #[test]
    fn overflow_preserves_completed_fragments_and_buffer_remains_usable() {
        let mut text = Text::<4>::new();
        text.write_str("ab").unwrap();
        assert!(text.write_str("cde").is_err());
        assert_eq!(text.bytes(), b"ab");
        write!(text, "{}", 12).unwrap();
        assert_eq!(text.as_ref(), b"ab12");
    }

    #[test]
    fn capacity_counts_utf8_bytes_and_never_splits_a_codepoint() {
        let mut text = Text::<3>::new();
        text.write_str("é").unwrap();
        assert!(text.write_str("é").is_err());
        assert_eq!(text.bytes(), "é".as_bytes());
        text.write_char('!').unwrap();
        assert_eq!(text.bytes(), "é!".as_bytes());
    }

    #[test]
    fn zero_capacity_accepts_only_empty_writes() {
        let mut text = Text::<0>::default();
        text.write_str("").unwrap();
        assert!(text.write_char('x').is_err());
        assert!(text.bytes().is_empty());
    }
}
