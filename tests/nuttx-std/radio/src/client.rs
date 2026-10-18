//! Overlap evidence uses only completed, byte-verified network echoes.
use ble_client_fixture::Progress;
use wifi::Error;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct Span {
    pub first_ms: u64,
    pub last_ms: u64,
    pub first_echoes: u32,
    pub last_echoes: u32,
}

#[derive(Default)]
pub(super) struct Overlap {
    first: Option<(u64, u32)>,
    span: Option<Span>,
}

impl Overlap {
    /// True authorizes the traffic worker's start gate, exactly once.
    pub fn record(&mut self, progress: Progress, echoes: u32) -> Result<bool, Error> {
        match progress {
            Progress::Poll => Ok(false),
            Progress::FirstWrite { elapsed_ms } if self.first.is_none() => {
                self.first = Some((elapsed_ms, echoes));
                Ok(true)
            }
            Progress::LastRead { elapsed_ms, pairs }
                if pairs == super::GATT_ROUNDS && self.span.is_none() =>
            {
                let (first_ms, first_echoes) = self.first.ok_or(Error::Protocol)?;
                if elapsed_ms < first_ms || echoes.saturating_sub(first_echoes) <= 5 {
                    return Err(Error::Protocol);
                }
                self.span = Some(Span {
                    first_ms,
                    last_ms: elapsed_ms,
                    first_echoes,
                    last_echoes: echoes,
                });
                Ok(false)
            }
            _ => Err(Error::Protocol),
        }
    }

    pub fn finish(&self) -> Result<Span, Error> {
        self.span.ok_or(Error::Protocol)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_gatt_data_interval_counts_as_overlap() {
        let mut overlap = Overlap::default();
        assert_eq!(overlap.record(Progress::Poll, 500), Ok(false));
        assert_eq!(
            overlap.record(Progress::FirstWrite { elapsed_ms: 10 }, 500),
            Ok(true)
        );
        assert_eq!(overlap.record(Progress::Poll, 505), Ok(false));
        assert_eq!(
            overlap.record(
                Progress::LastRead {
                    elapsed_ms: 20,
                    pairs: 100
                },
                506
            ),
            Ok(false)
        );
        assert_eq!(
            overlap.finish(),
            Ok(Span {
                first_ms: 10,
                last_ms: 20,
                first_echoes: 500,
                last_echoes: 506
            })
        );
    }

    #[test]
    fn partial_duplicate_and_out_of_order_progress_is_not_success() {
        let mut overlap = Overlap::default();
        assert_eq!(overlap.finish(), Err(Error::Protocol));
        assert_eq!(
            overlap.record(
                Progress::LastRead {
                    elapsed_ms: 20,
                    pairs: 100
                },
                6
            ),
            Err(Error::Protocol)
        );
        overlap
            .record(Progress::FirstWrite { elapsed_ms: 10 }, 0)
            .unwrap();
        assert_eq!(overlap.finish(), Err(Error::Protocol));
        assert_eq!(
            overlap.record(Progress::FirstWrite { elapsed_ms: 11 }, 0),
            Err(Error::Protocol)
        );
        assert_eq!(
            overlap.record(
                Progress::LastRead {
                    elapsed_ms: 20,
                    pairs: 99
                },
                6
            ),
            Err(Error::Protocol)
        );
        overlap
            .record(
                Progress::LastRead {
                    elapsed_ms: 20,
                    pairs: 100,
                },
                6,
            )
            .unwrap();
        assert_eq!(
            overlap.record(
                Progress::LastRead {
                    elapsed_ms: 21,
                    pairs: 100
                },
                7
            ),
            Err(Error::Protocol)
        );
    }

    #[test]
    fn stopped_wrapped_or_insufficient_network_counts_are_not_overlap() {
        for (first, last, time) in [(0, 0, 20), (0, 5, 20), (10, 9, 20), (0, 6, 9)] {
            let mut overlap = Overlap::default();
            overlap
                .record(Progress::FirstWrite { elapsed_ms: 10 }, first)
                .unwrap();
            assert_eq!(
                overlap.record(
                    Progress::LastRead {
                        elapsed_ms: time,
                        pairs: 100
                    },
                    last
                ),
                Err(Error::Protocol)
            );
            assert_eq!(overlap.finish(), Err(Error::Protocol));
        }
    }
}
