//! Descriptor ownership and bounded progress; OS source selection is a factory.
use crate::sdk::{Entropy, EntropyUnavailable};
use std::{
    fs::{File, OpenOptions},
    io::{ErrorKind, Read},
    os::unix::fs::OpenOptionsExt,
};

pub(crate) struct Source(File);
impl Source {
    pub(crate) fn open(path: &str, flags: i32) -> Result<Self, crate::Error> {
        OpenOptions::new()
            .read(true)
            .custom_flags(flags)
            .open(path)
            .map(Self)
            .map_err(|_| crate::Error::Entropy)
    }
}
impl Entropy for Source {
    fn fill(&mut self, output: &mut [u8]) -> Result<(), EntropyUnavailable> {
        let mut done = 0;
        let mut interrupted = 0;
        while done < output.len() {
            match self.0.read(&mut output[done..]) {
                Ok(0) => return Err(EntropyUnavailable),
                Ok(n) => {
                    done += n;
                    interrupted = 0;
                }
                Err(error) if error.kind() == ErrorKind::Interrupted && interrupted < 7 => {
                    interrupted += 1
                }
                Err(_) => return Err(EntropyUnavailable),
            }
        }
        Ok(())
    }
}
