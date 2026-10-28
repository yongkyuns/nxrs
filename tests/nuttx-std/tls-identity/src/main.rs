//! Ordinary-main qualification fixture; outputs only a public enrollment request.
#![forbid(unsafe_code)]

fn main() {
    let mut identity = tls::open().expect("qualified entropy and volatile identity");
    assert!(matches!(tls::open(), Err(tls::Error::Busy)));
    identity.refresh_entropy().expect("fresh entropy");
    let mut output = [0u8; 1024];
    let length = identity
        .request("qualification", &mut output)
        .expect("public CSR");
    print!(
        "{}",
        std::str::from_utf8(&output[..length]).expect("PEM ASCII")
    );
    drop(identity);
    drop(tls::open().expect("identity cleanup and reacquisition"));
    println!("NXRS_TLS_IDENTITY_PASS");
}
