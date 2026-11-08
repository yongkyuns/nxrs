#![cfg(unix)]
mod support;
use embedded_hal::spi::{Operation, SpiDevice};
use std::sync::Mutex;

static CALLS: Mutex<Vec<Vec<u8>>> = Mutex::new(Vec::new());
mod spi_ffi {
    pub fn transfer(
        _: i32,
        select: u16,
        frequency: u32,
        mode: u8,
        tx: &[u8],
        rx: &mut [u8],
    ) -> std::io::Result<()> {
        assert_eq!((select, frequency, mode), (7, 4_000_000, 3));
        super::CALLS.lock().unwrap().push(tx.to_vec());
        if tx == [0xff] {
            return Err(std::io::ErrorKind::TimedOut.into());
        }
        for (i, byte) in rx.iter_mut().enumerate() {
            *byte = i as u8;
        }
        Ok(())
    }
}

#[test]
fn one_exchange_preserves_cs_and_validates_all_operations_before_io() {
    use support::{Config, Device, Error};
    let path = std::env::temp_dir().join(format!("nxrs-spi-test-{}", std::process::id()));
    let file = std::fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&path)
        .unwrap();
    let config = Config {
        path: path.to_str().unwrap(),
        chip_select: 7,
        frequency: 4_000_000,
        mode: 3,
    };
    let mut device = Device::open(config).unwrap();
    let mut read = [0xff; 2];
    let mut in_place = [20, 21];
    let mut short = [0xff];
    let mut long = [0xff; 3];
    device
        .transaction(&mut [
            Operation::Write(&[0x80]),
            Operation::Read(&mut read),
            Operation::TransferInPlace(&mut in_place),
            Operation::Transfer(&mut short, &[30, 31]),
            Operation::Transfer(&mut long, &[40]),
        ])
        .unwrap();
    assert_eq!(
        (read, in_place, short, long),
        ([1, 2], [3, 4], [5], [7, 8, 9])
    );
    assert_eq!(
        *CALLS.lock().unwrap(),
        [vec![0x80, 0, 0, 20, 21, 30, 31, 40, 0, 0]]
    );
    for mut operations in [
        vec![Operation::Write(&[1]), Operation::DelayNs(1)],
        vec![Operation::Write(&[0; 256]), Operation::Write(&[1])],
    ] {
        assert_eq!(device.transaction(&mut operations), Err(Error::Unsupported));
    }
    device.transaction(&mut []).unwrap();
    assert_eq!(CALLS.lock().unwrap().len(), 1);
    let mut failed = [0xff];
    assert_eq!(device.transfer_in_place(&mut failed), Err(Error::Io));
    assert_eq!(failed, [0xff]); // No partial publication after a failed exchange.
    for invalid in [
        Config { mode: 4, ..config },
        Config {
            frequency: 0,
            ..config
        },
        Config { path: "", ..config },
    ] {
        assert!(matches!(Device::open(invalid), Err(Error::InvalidConfig)));
    }
    drop((device, file));
    std::fs::remove_file(path).unwrap();
}
