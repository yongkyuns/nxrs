//! Bounded inactive-slot transfer. No image parser or cryptography lives here.
use crate::DeviceError;

pub(crate) trait Slot {
    fn capacity(&self) -> u32;
    fn alignment(&self) -> u32;
    fn erase(&mut self) -> Result<(), DeviceError>;
    fn write(&mut self, offset: u32, data: &[u8]) -> Result<(), DeviceError>;
    fn read(&mut self, offset: u32, data: &mut [u8]) -> Result<(), DeviceError>;
}

pub(crate) fn copy(
    slot: &mut impl Slot,
    source: &mut dyn std::io::Read,
    length: u32,
) -> Result<(), DeviceError> {
    let align = slot.alignment();
    if align == 0 || !align.is_power_of_two() || align > 256 {
        return Err(DeviceError::InvalidState);
    }
    if length < 32 {
        return Err(DeviceError::InvalidInput);
    }
    let padded = length.checked_add(align - 1).ok_or(DeviceError::Capacity)? & !(align - 1);
    if padded > slot.capacity() {
        return Err(DeviceError::Capacity);
    }
    slot.erase()?;
    let mut expected = [0xff; 256];
    let mut actual = [0; 256];
    let mut offset = 0;
    while offset < length {
        let count = (length - offset).min(256) as usize;
        let span = (count + align as usize - 1) & !(align as usize - 1);
        expected.fill(0xff);
        source
            .read_exact(&mut expected[..count])
            .map_err(|_| DeviceError::Io)?;
        slot.write(offset, &expected[..span])?;
        slot.read(offset, &mut actual[..span])?;
        if actual[..span] != expected[..span] {
            return Err(DeviceError::Io);
        }
        offset += count as u32;
    }
    let mut extra = [0];
    loop {
        match source.read(&mut extra) {
            Ok(0) => return Ok(()),
            Ok(_) => return Err(DeviceError::InvalidInput),
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => return Err(DeviceError::Io),
        }
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    pub(crate) struct Memory {
        bytes: [u8; 1024],
        erased: bool,
        fail: u8,
    }
    impl Memory {
        pub(crate) fn new() -> Self {
            Self {
                bytes: [0x5a; 1024],
                erased: false,
                fail: 0,
            }
        }
    }
    impl Slot for Memory {
        fn capacity(&self) -> u32 {
            768
        }
        fn alignment(&self) -> u32 {
            32
        }
        fn erase(&mut self) -> Result<(), DeviceError> {
            self.erased = true;
            if self.fail == 1 {
                return Err(DeviceError::Io);
            }
            self.bytes.fill(0xff);
            Ok(())
        }
        fn write(&mut self, offset: u32, data: &[u8]) -> Result<(), DeviceError> {
            assert_eq!(offset % 32, 0);
            assert_eq!(data.len() % 32, 0);
            assert!(offset as usize + data.len() <= 768);
            if self.fail == 2 {
                return Err(DeviceError::Io);
            }
            self.bytes[offset as usize..offset as usize + data.len()].copy_from_slice(data);
            Ok(())
        }
        fn read(&mut self, offset: u32, data: &mut [u8]) -> Result<(), DeviceError> {
            if self.fail == 3 {
                return Err(DeviceError::Io);
            }
            data.copy_from_slice(&self.bytes[offset as usize..offset as usize + data.len()]);
            if self.fail == 4 {
                data[0] ^= 1;
            }
            Ok(())
        }
    }
    #[test]
    fn fragmented_input_tail_padding_and_metadata_exclusion() {
        struct Fragments<'a>(&'a [u8], bool);
        impl std::io::Read for Fragments<'_> {
            fn read(&mut self, output: &mut [u8]) -> std::io::Result<usize> {
                if self.0.is_empty() && self.1 {
                    self.1 = false;
                    return Err(std::io::ErrorKind::Interrupted.into());
                }
                let length = output.len().min(7);
                std::io::Read::read(&mut self.0, &mut output[..length])
            }
        }
        for length in [32, 255, 256, 257, 737, 768] {
            let data = [0x39; 768];
            let mut slot = Memory::new();
            copy(
                &mut slot,
                &mut Fragments(&data[..length], true),
                length as u32,
            )
            .unwrap();
            assert_eq!(&slot.bytes[..length], &data[..length]);
            assert!(slot.bytes[length..].iter().all(|byte| *byte == 0xff));
        }
    }
    #[test]
    fn bounds_transfer_errors_and_readback_mismatch_refuse_completion() {
        for (length, error) in [
            (0, DeviceError::InvalidInput),
            (31, DeviceError::InvalidInput),
            (769, DeviceError::Capacity),
            (u32::MAX, DeviceError::Capacity),
        ] {
            let mut slot = Memory::new();
            assert_eq!(copy(&mut slot, &mut &b""[..], length), Err(error));
            assert!(!slot.erased);
        }
        for (input, error) in [(31, DeviceError::Io), (33, DeviceError::InvalidInput)] {
            assert_eq!(
                copy(&mut Memory::new(), &mut &[0; 33][..input], 32),
                Err(error)
            );
        }
        for fail in 1..=4 {
            let mut slot = Memory::new();
            slot.fail = fail;
            assert_eq!(copy(&mut slot, &mut &[0; 32][..], 32), Err(DeviceError::Io));
        }
    }
}
