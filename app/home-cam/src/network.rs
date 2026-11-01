//! One versioned Wi-Fi profile. Load/save only with the radio and camera idle.
use config::Store;
use wifi::{Command, Credentials, Passphrase, Ssid};

const KEY: &str = "camera.wifi";
const MAX_RECORD: usize = 4 + 2 + 32 + 63;

#[derive(Debug, PartialEq, Eq)]
pub enum Error {
    InvalidProfile,
    Store(config::Error),
}

pub struct Profile {
    ssid: Ssid,
    password: [u8; 63],
    length: usize,
}

impl Profile {
    pub fn new(ssid: &[u8], password: &[u8]) -> Result<Self, Error> {
        let ssid = Ssid::new(ssid).map_err(|_| Error::InvalidProfile)?;
        if password.len() > 63 {
            return Err(Error::InvalidProfile);
        }
        let mut profile = Self {
            ssid,
            password: [0; 63],
            length: password.len(),
        };
        profile.password[..password.len()].copy_from_slice(password);
        profile.command().map_err(|_| Error::InvalidProfile)?;
        Ok(profile)
    }

    pub fn command(&self) -> Result<Command, wifi::Error> {
        let credentials = if self.length == 0 {
            Credentials::Open
        } else {
            let text = std::str::from_utf8(&self.password[..self.length])
                .map_err(|_| wifi::Error::InvalidConfig)?;
            Credentials::Wpa2Personal(Passphrase::new(text)?)
        };
        Ok(Command::Connect {
            ssid: self.ssid,
            credentials,
        })
    }

    pub fn load(store: &mut impl Store) -> Result<Option<Self>, Error> {
        let mut bytes = [0; MAX_RECORD];
        let Some(length) = store.get(KEY, &mut bytes).map_err(Error::Store)? else {
            return Ok(None);
        };
        let bytes = bytes.get(..length).ok_or(Error::InvalidProfile)?;
        if bytes.len() < 6
            || &bytes[..4] != b"NWP1"
            || bytes.len() != 6 + usize::from(bytes[4]) + usize::from(bytes[5])
            || bytes[4] > 32
        {
            return Err(Error::InvalidProfile);
        }
        let end = 6 + usize::from(bytes[4]);
        Self::new(&bytes[6..end], &bytes[end..]).map(Some)
    }

    /// Call after a successful join and joined radio retirement. Readback is
    /// checked; power-loss durability remains a property of the native store.
    pub fn save(&self, store: &mut impl Store) -> Result<(), Error> {
        let mut bytes = [0; MAX_RECORD];
        bytes[..4].copy_from_slice(b"NWP1");
        bytes[4] = self.ssid.as_bytes().len() as u8;
        bytes[5] = self.length as u8;
        let end = 6 + self.ssid.as_bytes().len();
        bytes[6..end].copy_from_slice(self.ssid.as_bytes());
        bytes[end..end + self.length].copy_from_slice(&self.password[..self.length]);
        let value = &bytes[..end + self.length];
        store.set(KEY, value).map_err(Error::Store)?;
        let mut observed = [0; MAX_RECORD];
        let length = store.get(KEY, &mut observed).map_err(Error::Store)?;
        if length != Some(value.len()) || &observed[..value.len()] != value {
            return Err(Error::Store(config::Error::Io));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[derive(Default)]
    struct Memory(Option<Vec<u8>>, Option<config::Error>, usize);
    impl Store for Memory {
        fn get(&mut self, _: &str, out: &mut [u8]) -> Result<Option<usize>, config::Error> {
            if let Some(error) = self.1 {
                return Err(error);
            }
            self.0
                .as_ref()
                .map(|v| {
                    if v.len() > out.len() {
                        return Err(config::Error::BufferTooSmall);
                    }
                    out[..v.len()].copy_from_slice(v);
                    Ok(v.len())
                })
                .transpose()
        }
        fn set(&mut self, _: &str, value: &[u8]) -> Result<(), config::Error> {
            if let Some(error) = self.1 {
                return Err(error);
            }
            self.2 += 1;
            self.0 = Some(value.to_vec());
            Ok(())
        }
        fn remove(&mut self, _: &str) -> Result<(), config::Error> {
            self.0 = None;
            Ok(())
        }
    }
    #[test]
    fn bounded_profiles_roundtrip_without_boot_writes() {
        let mut store = Memory::default();
        assert!(Profile::load(&mut store).unwrap().is_none());
        for (ssid, password) in [
            (&b"\xff\0"[..], &b""[..]),
            (&[b's'; 32][..], &[b'p'; 63][..]),
        ] {
            Profile::new(ssid, password)
                .unwrap()
                .save(&mut store)
                .unwrap();
            let loaded = Profile::load(&mut store).unwrap().unwrap();
            assert_eq!(loaded.ssid.as_bytes(), ssid);
            assert_eq!(&loaded.password[..loaded.length], password);
        }
        assert_eq!(store.2, 2);
    }
    #[test]
    fn invalid_and_failed_reads_are_not_unprovisioned() {
        for bytes in [
            b"".to_vec(),
            b"NWP2\x01\x00x".to_vec(),
            b"NWP1\x00\x00".to_vec(),
            b"NWP1\x01\x01xp".to_vec(),
            b"NWP1\x01\x00xx".to_vec(),
            vec![0; MAX_RECORD + 1],
        ] {
            assert!(Profile::load(&mut Memory(Some(bytes), None, 0)).is_err());
        }
        for error in [config::Error::Io, config::Error::Full] {
            let mut store = Memory(None, Some(error), 0);
            assert!(matches!(Profile::load(&mut store), Err(Error::Store(e)) if e == error));
            assert_eq!(
                Profile::new(b"test", b"password").unwrap().save(&mut store),
                Err(Error::Store(error))
            );
        }
    }
}
