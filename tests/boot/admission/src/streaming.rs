//! Real incremental SDK SHA and bounded source policy checks, never signature authority.
use super::{Case, Expected};
use firmware_image::{
    check_policy, FormatError, IncrementalHash, PolicyChecked, PolicyError, ReadAt, Trust,
};
use nxrs_image_admission_qualification::native::{NativeError, NativeSha};

#[derive(Debug, PartialEq, Eq)]
enum ReadFailure {
    Injected,
    Bounds,
}
struct Region<'a> {
    bytes: &'a [u8],
    extent: usize,
    calls: usize,
    fail_on: usize,
    largest: usize,
}
impl<'a> Region<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        Self {
            bytes,
            extent: bytes.len(),
            calls: 0,
            fail_on: 0,
            largest: 0,
        }
    }
}
impl ReadAt for Region<'_> {
    type Error = ReadFailure;
    fn length(&self) -> usize {
        self.extent
    }
    fn read_at(&mut self, offset: usize, output: &mut [u8]) -> Result<(), ReadFailure> {
        self.calls += 1;
        self.largest = self.largest.max(output.len());
        assert!(output.len() <= 128, "bounded policy source read");
        if self.fail_on == self.calls {
            return Err(ReadFailure::Injected);
        }
        self.bytes
            .read_at(offset, output)
            .map_err(|_| ReadFailure::Bounds)
    }
}

pub(super) fn case(case: &Case, trust: &Trust<'_>, sha: &mut NativeSha) -> Option<PolicyChecked> {
    let mut reader = Region::new(case.image);
    let result = check_policy(&mut reader, trust, sha);
    let expected = match case.expected {
        // Policy checks do not validate signature, floor, frame or boot trailer.
        Expected::Crypto | Expected::Accept | Expected::Format(FormatError::SecurityFloor) => {
            Expected::Accept
        }
        Expected::Format(FormatError::Tlv) if case.label == "trailing-preconfirmation" => {
            Expected::Accept
        }
        other => other,
    };
    let (actual, value) = match result {
        Ok(value) => {
            assert!(value.container_bytes() <= case.image.len());
            if case.label != "trailing-preconfirmation" {
                assert_eq!(value.container_bytes(), case.image.len());
            }
            (Expected::Accept, Some(value))
        }
        Err(PolicyError::Format(error)) => (Expected::Format(error), None),
        error => panic!("unexpected source/hash failure: {error:?}"),
    };
    assert_eq!(actual, expected, "streaming policy {}", case.label);
    println!("IMAGE_POLICY_CASE {} passed", case.label);
    value
}

#[derive(Debug, PartialEq, Eq)]
enum HashFailure {
    Injected,
    Native(NativeError),
}
struct FaultHash<'a> {
    native: &'a mut NativeSha,
    calls: usize,
    fail_on: usize,
}
impl FaultHash<'_> {
    fn check(&mut self) -> Result<(), HashFailure> {
        self.calls += 1;
        if self.calls == self.fail_on {
            Err(HashFailure::Injected)
        } else {
            Ok(())
        }
    }
}
impl IncrementalHash for FaultHash<'_> {
    type Error = HashFailure;
    fn reset(&mut self) -> Result<(), HashFailure> {
        self.check()?;
        self.native.reset().map_err(HashFailure::Native)
    }
    fn update(&mut self, bytes: &[u8]) -> Result<(), HashFailure> {
        self.check()?;
        self.native.update(bytes).map_err(HashFailure::Native)
    }
    fn finish(&mut self) -> Result<[u8; 32], HashFailure> {
        self.check()?;
        self.native.finish().map_err(HashFailure::Native)
    }
}

pub(super) fn faults(valid: &[u8], trust: &Trust<'_>, sha: &mut NativeSha) {
    let mut reader = Region::new(valid);
    let mut hash = FaultHash {
        native: sha,
        calls: 0,
        fail_on: 0,
    };
    let expected = check_policy(&mut reader, trust, &mut hash).unwrap();
    let (reads, hashes) = (reader.calls, hash.calls);
    assert!(reads > 0 && hashes > 0 && reader.largest == 128);
    for fail_on in 1..=reads {
        let mut reader = Region::new(valid);
        reader.fail_on = fail_on;
        assert!(matches!(
            check_policy(&mut reader, trust, sha),
            Err(PolicyError::Read(ReadFailure::Injected))
        ));
    }
    for fail_on in 1..=hashes {
        let mut reader = Region::new(valid);
        let mut hash = FaultHash {
            native: sha,
            calls: 0,
            fail_on,
        };
        assert!(matches!(
            check_policy(&mut reader, trust, &mut hash),
            Err(PolicyError::Hash(HashFailure::Injected))
        ));
    }
    // A slot can contain control state after the container. Policy neither reads
    // nor authorizes that state: a caller must preserve its bootloader's checks.
    let mut reader = Region::new(valid);
    reader.extent = trust.layout.slot_bytes as usize;
    let restored = check_policy(&mut reader, trust, sha).unwrap();
    assert_eq!(restored.container_bytes(), valid.len());
    assert_eq!(restored.payload_sha256(), expected.payload_sha256());
    for length in 0..valid.len() {
        assert!(
            check_policy(&mut Region::new(&valid[..length]), trust, sha).is_err(),
            "policy prefix {length}"
        );
    }
    println!("IMAGE_POLICY_FAULTS reads={reads} hashes={hashes} max_read=128 restored=true");
}
