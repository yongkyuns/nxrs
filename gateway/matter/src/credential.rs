//! Input names and private service credentials shared by local gateway clients.
use std::{io::Read, path::Path};

pub(crate) fn identifier(s: &str) -> bool {
    (1..=64).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
}

pub(crate) fn read(path: &Path) -> Result<String, &'static str> {
    let mut token = String::new();
    crate::platform::open_file(path, false, false)
        .and_then(|file| file.take(66).read_to_string(&mut token))
        .map_err(|_| "unreadable gateway credential")?;
    if token.ends_with('\n') {
        token.pop();
    }
    if token.len() != 64
        || !token
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err("invalid gateway credential");
    }
    Ok(token)
}
