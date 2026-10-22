use firmware_image::{authenticate, Crypto, Error, FormatError, Trust};
use nxrs_image_admission_qualification::native::Native;
use std::num::NonZeroU32;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Expected {
    Accept,
    Format(FormatError),
    Crypto,
}
struct Case {
    label: &'static str,
    image: &'static [u8],
    floor: u32,
    expected: Expected,
    native_signed_end: Option<usize>,
}
include!(env!("NXRS_ADMISSION_CASES"));

fn main() -> std::process::ExitCode {
    std::process::ExitCode::from(run())
}

fn run() -> u8 {
    let mut arguments = std::env::args().skip(1);
    let mode = arguments.next();
    if arguments.next().is_some() || !matches!(mode.as_deref(), None | Some("floor-refusal")) {
        return 2;
    }
    let mut crypto = Native::new(&MODULUS).expect("actual SDK public key import");
    assert_eq!(
        crypto.sha256(b"abc").unwrap(),
        [
            0xba, 0x78, 0x16, 0xbf, 0x8f, 0x01, 0xcf, 0xea, 0x41, 0x41, 0x40, 0xde, 0x5d, 0xae,
            0x22, 0x23, 0xb0, 0x03, 0x61, 0xa3, 0x96, 0x17, 0x7a, 0x9c, 0xb4, 0x10, 0xff, 0x61,
            0xf2, 0x00, 0x15, 0xad
        ]
    );
    let trust = trusted();
    if mode.as_deref() == Some("floor-refusal") {
        // Real authenticated positive image, refused by the higher stored floor.
        // Return normally so the SDK context is destroyed before process exit.
        assert!(matches!(
            authenticate(
                CASES[0].image,
                &trust,
                NonZeroU32::new(8).unwrap(),
                &mut crypto
            ),
            Err(Error::Format(FormatError::SecurityFloor))
        ));
        println!("IMAGE_ADMISSION_EXPECTED_REFUSAL floor");
        return 7;
    }
    for case in CASES {
        if let Some(end) = case.native_signed_end {
            let digest = crypto.sha256(&case.image[..end]).unwrap();
            // Independently exercise the real RSA verifier before nxrs policy admission.
            let signature = case.image[case.image.len() - 384..].try_into().unwrap();
            crypto
                .verify_rsa3072_pss_sha256(&digest, signature)
                .expect(case.label);
        }
        let result = authenticate(
            case.image,
            &trust,
            NonZeroU32::new(case.floor).unwrap(),
            &mut crypto,
        );
        let actual = match result {
            Ok(image) => {
                assert_eq!(image.bytes(), case.image);
                assert_eq!(image.security_counter().get() >= case.floor, true);
                assert_eq!(
                    crypto.sha256(image.payload()).unwrap(),
                    image.payload_sha256()
                );
                Expected::Accept
            }
            Err(Error::Format(error)) => Expected::Format(error),
            Err(Error::Crypto(_)) => Expected::Crypto,
        };
        assert_eq!(actual, case.expected, "{}", case.label);
        println!("IMAGE_ADMISSION_CASE {} passed", case.label);
    }
    let valid = CASES[0].image;
    for length in 0..valid.len() {
        assert!(
            authenticate(
                &valid[..length],
                &trust,
                NonZeroU32::new(1).unwrap(),
                &mut crypto
            )
            .is_err(),
            "prefix {length}"
        );
    }
    // Bounded deterministic corruptions execute the real parser and crypto, with
    // no substitute authenticator and no accepting fallback on SDK failures.
    let mut seed = 0x9f62_c1a5_u32;
    for _ in 0..10000 {
        seed = seed.wrapping_mul(1664525).wrapping_add(1013904223);
        let offset = seed as usize % valid.len();
        let mut mutated = valid.to_vec();
        mutated[offset] ^= 1 << (seed >> 29);
        assert!(authenticate(&mutated, &trust, NonZeroU32::new(1).unwrap(), &mut crypto).is_err());
    }
    // Trusted geometry corruption must be refused before any admission.
    for changed in [
        firmware_image::Layout {
            primary_offset: trust.layout.secondary_offset,
            ..trust.layout
        },
        firmware_image::Layout {
            erase_bytes: 0,
            ..trust.layout
        },
        firmware_image::Layout {
            max_sectors: 1,
            ..trust.layout
        },
        firmware_image::Layout {
            scratch_offset: u32::MAX,
            ..trust.layout
        },
    ] {
        let invalid = Trust {
            layout: changed,
            ..trust
        };
        assert!(matches!(
            authenticate(valid, &invalid, NonZeroU32::new(1).unwrap(), &mut crypto),
            Err(Error::Format(FormatError::Policy))
        ));
    }
    println!(
        "IMAGE_ADMISSION_QUALIFIED cases={} proper_prefixes={} corruptions=10000",
        CASES.len(),
        valid.len()
    );
    0
}
