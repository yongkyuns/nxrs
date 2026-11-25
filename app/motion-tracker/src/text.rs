use std::fmt;

pub struct Text<const N: usize> {
    bytes: [u8; N],
    len: usize,
}

impl<const N: usize> Text<N> {
    pub fn new() -> Self {
        Self {
            bytes: [0; N],
            len: 0,
        }
    }

    pub fn bytes(&self) -> &[u8] {
        &self.bytes[..self.len]
    }
}

impl<const N: usize> fmt::Write for Text<N> {
    fn write_str(&mut self, text: &str) -> fmt::Result {
        let end = self.len + text.len();
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
