//! Real Rust resource adapter over a fake synchronous target ABI.
#![cfg(unix)]
use embedded_hal::i2c::{I2c, Operation};
use i2c::{Config, Error};
use std::{fs, io, sync::Mutex};
#[path = "support/mod.rs"]
mod resource;

type Transfer = (u8, u32, Vec<u8>, usize);
static CALLS: Mutex<Vec<Transfer>> = Mutex::new(Vec::new());
mod ffi {
    use super::*;
    pub(super) fn transfer(
        _: i32,
        address: u8,
        frequency: u32,
        write: &[u8],
        read: &mut [u8],
    ) -> io::Result<()> {
        CALLS
            .lock()
            .unwrap()
            .push((address, frequency, write.to_vec(), read.len()));
        if write == [0xff] {
            return Err(io::Error::other("injected transfer failure"));
        }
        read.fill(42);
        Ok(())
    }
}

#[test]
fn independent_endpoints_preserve_transactions_and_reject_unsupported_io() {
    let path = std::env::temp_dir().join(format!("nxrs-i2c-{}", std::process::id()));
    fs::write(&path, []).unwrap();
    let config = Config {
        path: path.to_str().unwrap(),
        frequency: 400_000,
    };
    assert!(matches!(
        resource::Bus::open(Config {
            frequency: 0,
            ..config
        }),
        Err(Error::InvalidConfig)
    ));
    let mut first = resource::Bus::open(config).unwrap();
    let mut second = resource::Bus::open(Config {
        frequency: 100_000,
        ..config
    })
    .unwrap();
    let mut read = [0; 2];
    first.write_read(0x68, &[0x10], &mut read).unwrap();
    assert_eq!(read, [42; 2]);
    second.write(0x40, &[1, 2]).unwrap();
    first.read(0x69, &mut read).unwrap();
    assert_eq!(
        *CALLS.lock().unwrap(),
        [
            (0x68, 400_000, vec![0x10], 2),
            (0x40, 100_000, vec![1, 2], 0),
            (0x69, 400_000, vec![], 2)
        ]
    );
    first.transaction(0x68, &mut []).unwrap();
    assert_eq!(first.write(0x80, &[0]), Err(Error::InvalidConfig));
    assert_eq!(first.write(0x68, &[0; 65]), Err(Error::Unsupported));
    assert_eq!(
        first.transaction(
            0x68,
            &mut [Operation::Read(&mut read), Operation::Write(&[0])]
        ),
        Err(Error::Unsupported)
    );
    assert_eq!(
        CALLS.lock().unwrap().len(),
        3,
        "rejected operations never reach ioctl"
    );
    assert_eq!(first.write(0x68, &[0xff]), Err(Error::Io));
    drop((first, second));
    fs::remove_file(path).unwrap();
}
