//! Backend-independent credential inputs and trusted-time contract.
#![forbid(unsafe_code)]
use crate::Error;
use std::string::String;
use std::time::{Duration, Instant};

/// A short-lived authenticated UTC observation, advanced by monotonic time.
/// The caller must authenticate the source and bound delivery uncertainty before
/// construction. This does not make an unqualified device clock trusted.
pub struct TimeLease {
    pub(crate) epoch: i64,
    pub(crate) observed: Instant,
    pub(crate) expires: Instant,
    uncertainty: Duration,
}
impl TimeLease {
    pub fn from_trusted(unix_seconds: u64, max_age: Duration) -> Result<Self, Error> {
        Self::from_trusted_with_uncertainty(unix_seconds, max_age, Instant::now(), Duration::ZERO)
    }

    /// Constructs a lease for an authenticated UTC observation whose delivery
    /// may have taken up to `uncertainty` at `observed`. Preserve that receipt
    /// instant so subsequent scheduling delay cannot renew the observation.
    /// Uncertainty widens the upper bound and consumes the lease's lifetime.
    pub fn from_trusted_with_uncertainty(
        unix_seconds: u64,
        max_age: Duration,
        observed: Instant,
        uncertainty: Duration,
    ) -> Result<Self, Error> {
        if unix_seconds > 253_402_300_700
            || max_age.is_zero()
            || max_age > Duration::from_secs(60)
            || uncertainty >= max_age
            || observed > Instant::now()
        {
            return Err(Error::InvalidInput);
        }
        Ok(Self {
            epoch: unix_seconds as i64,
            observed,
            expires: observed + (max_age - uncertainty),
            uncertainty,
        })
    }
    pub(crate) fn sample(&self) -> Result<(i64, i64, Instant), Error> {
        let now = Instant::now();
        if now >= self.expires {
            return Err(Error::Time);
        }
        let elapsed = now
            .checked_duration_since(self.observed)
            .ok_or(Error::Time)?;
        let lower = self.epoch + elapsed.as_secs() as i64;
        // The observation reports whole seconds. Include delivery uncertainty
        // and round the upper bound outward; never extend validity.
        let upper_elapsed = elapsed + self.uncertainty;
        let upper = self.epoch
            + 1
            + upper_elapsed.as_secs() as i64
            + i64::from(upper_elapsed.subsec_nanos() != 0);
        Ok((lower, upper, now))
    }

    /// Conservatively map an inclusive whole-second expiry to a monotonic
    /// deadline, accounting for the observation's delivery uncertainty.
    pub(crate) fn deadline_before(&self, unix_seconds: i64) -> Result<Instant, Error> {
        let seconds = unix_seconds
            .checked_sub(self.epoch)
            .and_then(|delta| delta.checked_sub(1))
            .and_then(|delta| u64::try_from(delta).ok())
            .ok_or(Error::Time)?;
        let duration = Duration::from_secs(seconds)
            .checked_sub(self.uncertainty)
            .ok_or(Error::Time)?;
        let deadline = self.observed.checked_add(duration).ok_or(Error::Time)?;
        Ok(deadline.min(self.expires))
    }
}

/// Expected identities from independently approved enrollment, not from a CSR
/// or from the certificates being checked. Protected generation/rollback state
/// and the camera/site/gateway mapping remain the application's responsibility.
/// Each leaf must have exactly one matching URI among at most eight SAN entries.
/// Expected names contain 1..=256 printable ASCII bytes; SAN values are bounded
/// to 256 bytes. Other SAN types do not satisfy URI identity.
pub struct CertificatePolicy {
    pub issuer_sha256: [u8; 32],
    pub own_uri: String,
    pub peer_uri: String,
}
impl CertificatePolicy {
    pub fn validate(&self) -> Result<(), Error> {
        if [&self.own_uri, &self.peer_uri].iter().any(|name| {
            name.is_empty() || name.len() > 256 || !name.bytes().all(|b| (0x21..=0x7e).contains(&b))
        }) {
            return Err(Error::InvalidInput);
        }
        Ok(())
    }
}

/// Public DER enrollment material and its independently approved expectations.
/// This adapter checks signatures, revocation, validity and exact peer equality.
/// Leaves must declare exactly serverAuth for `own` and clientAuth for the peer;
/// missing or additional extended key usages are refused before the handshake.
pub struct Certificates<'a> {
    pub policy: &'a CertificatePolicy,
    pub own: &'a [u8],
    pub issuer: &'a [u8],
    pub approved_peer: &'a [u8],
    pub revocations: &'a [u8],
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn time_observations_bound_uncertainty_and_expire() {
        const MAX_TIME_AGE: Duration = Duration::from_secs(60);
        for age in [Duration::ZERO, MAX_TIME_AGE + Duration::from_secs(1)] {
            assert!(matches!(
                TimeLease::from_trusted(1_800_000_000, age),
                Err(Error::InvalidInput)
            ));
        }
        assert!(matches!(
            TimeLease::from_trusted_with_uncertainty(
                1_800_000_000,
                Duration::from_secs(5),
                Instant::now(),
                Duration::from_secs(5)
            ),
            Err(Error::InvalidInput)
        ));
        assert!(matches!(
            TimeLease::from_trusted(u64::MAX, MAX_TIME_AGE),
            Err(Error::InvalidInput)
        ));

        let epoch = 1_800_000_000i64;
        let max_age = Duration::from_secs(10);
        let uncertainty = Duration::from_millis(1_500);
        let observed = Instant::now() - Duration::from_millis(1_250);
        let mut time =
            TimeLease::from_trusted_with_uncertainty(epoch as u64, max_age, observed, uncertainty)
                .unwrap();
        assert_eq!(time.observed, observed);
        assert_eq!(
            time.expires.duration_since(time.observed),
            max_age - uncertainty
        );
        // Simulate 1.25 seconds since receipt without sleeping. Lower time floors
        // elapsed; upper time rounds elapsed plus uncertainty outward.
        let (lower, upper, _) = time.sample().unwrap();
        assert_eq!(lower, epoch + 1);
        assert_eq!(upper, epoch + 4);

        assert_eq!(
            time.deadline_before(epoch + 5)
                .unwrap()
                .duration_since(time.observed),
            Duration::from_millis(2_500),
        );
        assert!(time.deadline_before(epoch + 1).is_err());
        assert!(TimeLease::from_trusted_with_uncertainty(
            epoch as u64,
            max_age,
            Instant::now() + max_age,
            Duration::ZERO,
        )
        .is_err());

        let compatible = TimeLease::from_trusted(epoch as u64, max_age).unwrap();
        assert_eq!(compatible.uncertainty, Duration::ZERO);
        assert_eq!(
            compatible.expires.duration_since(compatible.observed),
            max_age
        );
        let (lower, upper, now) = compatible.sample().unwrap();
        let elapsed = now.duration_since(compatible.observed);
        assert_eq!(lower, epoch + elapsed.as_secs() as i64);
        assert_eq!(
            upper,
            epoch + 1 + elapsed.as_secs() as i64 + i64::from(elapsed.subsec_nanos() != 0)
        );
        time.expires = Instant::now();
        assert_eq!(time.sample(), Err(Error::Time));
    }
}
