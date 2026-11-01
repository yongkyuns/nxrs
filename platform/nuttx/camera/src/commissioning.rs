//! One-time product input. Saved records never acquire the console.
#![forbid(unsafe_code)]
use camera_stream::{
    bootstrap::{self, Commission},
    network::Profile,
};
use config::Store;
use std::{
    io::{self, Read, Write},
    sync::Arc,
    time::{Duration, Instant},
};

pub(crate) struct Settings {
    pub commission: Arc<Commission>,
    pub wifi: Profile,
    pub save_wifi: bool,
}

fn exact(input: &mut impl Read, mut bytes: &mut [u8], until: Instant) -> io::Result<()> {
    while !bytes.is_empty() {
        if Instant::now() >= until {
            return Err(io::ErrorKind::TimedOut.into());
        }
        match input.read(bytes) {
            Ok(0) => return Err(io::ErrorKind::UnexpectedEof.into()),
            Ok(n) => bytes = &mut bytes[n..],
            Err(e)
                if matches!(
                    e.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                ) =>
            {
                std::thread::sleep(Duration::from_millis(1))
            }
            Err(e) => return Err(e),
        }
    }
    Ok(())
}

pub(crate) fn load<I: Read + Write>(
    store: &mut impl Store,
    open: impl FnOnce() -> io::Result<I>,
) -> Result<Settings, String> {
    let saved = bootstrap::load(store).map_err(|e| format!("commission load {e:?}"))?;
    let wifi = Profile::load(store).map_err(|e| format!("Wi-Fi load {e:?}"))?;
    let mut input = if saved.is_none() || wifi.is_none() {
        Some(open().map_err(|e| format!("commissioning open {e:?}"))?)
    } else {
        None
    };
    let commission = match saved {
        Some(value) => value,
        None => {
            let input = input.as_mut().ok_or("missing commissioning input")?;
            let packet = read_commission(input).map_err(|e| format!("commission input {e:?}"))?;
            bootstrap::provision(store, &packet).map_err(|e| format!("commission save {e:?}"))?
        }
    };
    let save_wifi = wifi.is_none();
    let wifi = match wifi {
        Some(value) => {
            if let Some(input) = &mut input {
                writeln!(input, "NXRS_WIFI_CREDENTIALS_SAVED")
                    .map_err(|e| format!("commission acknowledgement {e:?}"))?;
            }
            value
        }
        None => read_wifi(input.as_mut().ok_or("missing Wi-Fi input")?)
            .map_err(|e| format!("Wi-Fi input {e:?}"))?,
    };
    // The native owner restores termios and closes before network initialization.
    drop(input);
    Ok(Settings {
        commission: Arc::new(commission),
        wifi,
        save_wifi,
    })
}

fn read_commission(input: &mut (impl Read + Write)) -> io::Result<Vec<u8>> {
    let maximum = Commission::MAX_BYTES;
    writeln!(input, "NXRS_BOOTSTRAP_PROVISION_READY max={maximum}")?;
    let until = Instant::now() + Duration::from_secs(30);
    let mut header = [0; 2];
    exact(input, &mut header, until)?;
    let length = u16::from_be_bytes(header) as usize;
    if length == 0 || length > maximum {
        return Err(io::ErrorKind::InvalidData.into());
    }
    let mut bytes = vec![0; length];
    let mut offset = 0;
    loop {
        writeln!(input, "NXRS_BOOTSTRAP_INPUT_ACK offset={offset}")?;
        if offset == length {
            break;
        }
        let end = (offset + 32).min(length);
        exact(input, &mut bytes[offset..end], until)?;
        offset = end;
    }
    Ok(bytes)
}

fn read_wifi(input: &mut (impl Read + Write)) -> io::Result<Profile> {
    writeln!(input, "NXRS_WIFI_CREDENTIALS_READY")?;
    let until = Instant::now() + Duration::from_secs(30);
    let mut lengths = [0; 2];
    exact(input, &mut lengths, until)?;
    let (ssid, password) = (lengths[0] as usize, lengths[1] as usize);
    if !(1..=32).contains(&ssid) || password > 63 {
        return Err(io::ErrorKind::InvalidData.into());
    }
    let mut bytes = [0; 95];
    exact(input, &mut bytes[..ssid + password], until)?;
    Profile::new(&bytes[..ssid], &bytes[ssid..ssid + password])
        .map_err(|_| io::ErrorKind::InvalidData.into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::Cell, collections::BTreeMap, io::Cursor, rc::Rc};
    #[derive(Default)]
    struct Memory(BTreeMap<String, Vec<u8>>);
    impl Store for Memory {
        fn get(&mut self, key: &str, out: &mut [u8]) -> Result<Option<usize>, config::Error> {
            self.0
                .get(key)
                .map(|v| {
                    out.get_mut(..v.len())
                        .ok_or(config::Error::BufferTooSmall)?
                        .copy_from_slice(v);
                    Ok(v.len())
                })
                .transpose()
        }
        fn set(&mut self, key: &str, value: &[u8]) -> Result<(), config::Error> {
            self.0.insert(key.into(), value.to_vec());
            Ok(())
        }
        fn remove(&mut self, key: &str) -> Result<(), config::Error> {
            self.0.remove(key);
            Ok(())
        }
    }
    struct Input {
        bytes: Cursor<Vec<u8>>,
        closed: Rc<Cell<bool>>,
        output: Rc<std::cell::RefCell<Vec<u8>>>,
    }
    impl Read for Input {
        fn read(&mut self, out: &mut [u8]) -> io::Result<usize> {
            self.bytes.read(out)
        }
    }
    impl Write for Input {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            self.output.borrow_mut().extend(bytes);
            Ok(bytes.len())
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }
    impl Drop for Input {
        fn drop(&mut self) {
            self.closed.set(true);
        }
    }
    fn packet() -> Vec<u8> {
        Commission {
            gateway: "192.168.1.2:8444".parse().unwrap(),
            certificate_sha256: [1; 32],
            token: [2; 32],
            site: "s".into(),
            camera: "c".into(),
            gateway_id: "g".into(),
        }
        .encode()
        .unwrap()
    }
    #[test]
    fn saved_records_skip_usb_and_missing_records_release_it_before_returning() {
        for (saved_commission, saved_wifi) in
            [(true, true), (false, true), (true, false), (false, false)]
        {
            let mut store = Memory::default();
            let packet = packet();
            if saved_commission {
                bootstrap::provision(&mut store, &packet).unwrap();
            }
            if saved_wifi {
                Profile::new(b"lab", b"secret123")
                    .unwrap()
                    .save(&mut store)
                    .unwrap();
            }
            let mut bytes = Vec::new();
            if !saved_commission {
                bytes.extend((packet.len() as u16).to_be_bytes());
                bytes.extend(&packet);
            }
            if !saved_wifi {
                bytes.extend(b"\x03\x09labsecret123");
            }
            let opened = Cell::new(false);
            let closed = Rc::new(Cell::new(false));
            let output = Rc::new(std::cell::RefCell::new(Vec::new()));
            let settings = load(&mut store, || {
                opened.set(true);
                Ok(Input {
                    bytes: Cursor::new(bytes),
                    closed: closed.clone(),
                    output: output.clone(),
                })
            })
            .unwrap();
            assert_eq!(opened.get(), !saved_commission || !saved_wifi);
            assert_eq!(closed.get(), opened.get());
            assert_eq!(settings.save_wifi, !saved_wifi);
            assert_eq!(settings.commission.camera, "c");
            assert!(settings.wifi.command().is_ok());
            assert!(bootstrap::load(&mut store).unwrap().is_some());
            // Wi-Fi is committed by the runtime only after a joined connection.
            assert_eq!(Profile::load(&mut store).unwrap().is_some(), saved_wifi);
            let text = String::from_utf8(output.borrow().clone()).unwrap();
            assert_eq!(
                text.contains("NXRS_BOOTSTRAP_PROVISION_READY"),
                !saved_commission
            );
            assert_eq!(text.contains("NXRS_WIFI_CREDENTIALS_READY"), !saved_wifi);
        }
    }
    #[test]
    fn corrupt_records_and_truncated_input_fail_without_silent_replacement() {
        let mut store = Memory::default();
        store.set("camera.bootstrap", b"corrupt").unwrap();
        assert!(
            load::<Cursor<Vec<u8>>>(&mut store, || panic!("corrupt record must not open USB"))
                .is_err()
        );
        assert_eq!(store.0["camera.bootstrap"], b"corrupt");
        store.0.clear();
        let closed = Rc::new(Cell::new(false));
        assert!(load(&mut store, || Ok(Input {
            bytes: Cursor::new(vec![0, 32, 1]),
            closed: closed.clone(),
            output: Default::default()
        }))
        .is_err());
        assert!(closed.get());
        assert!(store.0.is_empty());
    }
}
