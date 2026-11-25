//! SIM7080G AT commands used to establish an IPv4 PPP data session.
#![forbid(unsafe_code)]

/// Validate a DNS-style APN before interpolating it into an AT command.
pub fn valid_apn(apn: &str) -> bool {
    !apn.is_empty()
        && apn.len() <= 100
        && apn.split('.').all(|label| {
            let bytes = label.as_bytes();
            bytes.first().is_some_and(u8::is_ascii_alphanumeric)
                && bytes.last().is_some_and(u8::is_ascii_alphanumeric)
                && bytes
                    .iter()
                    .all(|byte| byte.is_ascii_alphanumeric() || *byte == b'-')
        })
}

pub fn context(apn: &str) -> Option<String> {
    valid_apn(apn).then(|| format!("AT+CGDCONT=1,\"IP\",\"{apn}\"\r"))
}

pub const DIAL: &[u8] = b"ATD*99#\r";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Reply {
    Ok,
    Connect,
    Rejected,
    Other,
}

pub fn reply(line: &[u8]) -> Reply {
    match line {
        b"OK" => Reply::Ok,
        b"CONNECT" => Reply::Connect,
        b"ERROR" | b"NO CARRIER" | b"BUSY" | b"NO DIALTONE" | b"NO ANSWER" => Reply::Rejected,
        line if line.starts_with(b"CONNECT ") => Reply::Connect,
        line if line.starts_with(b"+CME ERROR:") => Reply::Rejected,
        _ => Reply::Other,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn context_uses_one_ipv4_apn_and_rejects_injection() {
        assert_eq!(
            context("iot.ax-1").as_deref(),
            Some("AT+CGDCONT=1,\"IP\",\"iot.ax-1\"\r")
        );
        for bad in ["", "iot..org", "-iot", "iot\"\rAT", "µ", "iot."] {
            assert_eq!(context(bad), None);
        }
    }

    #[test]
    fn terminal_responses_are_distinct_from_unsolicited_lines() {
        assert_eq!(reply(b"+CEREG: 1"), Reply::Other);
        assert_eq!(reply(b"+CME ERROR: 30"), Reply::Rejected);
        assert_eq!(reply(b"CONNECT 115200"), Reply::Connect);
    }
}
