use settings::{Error, Store};
use std::time::Duration;

const KEY: &str = "motion.interval";
const VERSION: u8 = 1;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Interval(u16);

impl Interval {
    pub const DEFAULT: Self = Self(200);

    pub fn new(millis: u16) -> Option<Self> {
        (100..=2_000).contains(&millis).then_some(Self(millis))
    }

    pub fn duration(self) -> Duration {
        Duration::from_millis(u64::from(self.0))
    }

    pub fn load(store: &mut impl Store) -> Result<Self, Error> {
        let mut record = [0; 3];
        match store.get(KEY, &mut record)? {
            None => Ok(Self::DEFAULT),
            Some(3) if record[0] == VERSION => {
                Self::new(u16::from_le_bytes([record[1], record[2]])).ok_or(Error::InvalidInput)
            }
            _ => Err(Error::InvalidInput),
        }
    }

    pub fn save(self, store: &mut impl Store) -> Result<(), Error> {
        let [low, high] = self.0.to_le_bytes();
        store.set(KEY, &[VERSION, low, high])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Default)]
    struct Memory(Option<[u8; 3]>);

    impl Store for Memory {
        fn get(&mut self, key: &str, output: &mut [u8]) -> Result<Option<usize>, Error> {
            assert_eq!(key, KEY);
            Ok(self.0.map(|record| {
                output.copy_from_slice(&record);
                record.len()
            }))
        }

        fn set(&mut self, key: &str, value: &[u8]) -> Result<(), Error> {
            assert_eq!(key, KEY);
            self.0 = Some(value.try_into().unwrap());
            Ok(())
        }

        fn remove(&mut self, _: &str) -> Result<(), Error> {
            unreachable!()
        }
    }

    #[test]
    fn persists_an_explicit_update_and_rejects_invalid_records() {
        let mut store = Memory::default();
        assert_eq!(Interval::load(&mut store), Ok(Interval::DEFAULT));
        let interval = Interval::new(500).unwrap();
        interval.save(&mut store).unwrap();
        assert_eq!(Interval::load(&mut store), Ok(interval));
        for record in [[VERSION + 1, 244, 1], [VERSION, 0, 0]] {
            store.0 = Some(record);
            assert_eq!(Interval::load(&mut store), Err(Error::InvalidInput));
        }
    }
}
