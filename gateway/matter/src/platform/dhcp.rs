//! Read-only access to DHCP time options owned by the host network manager.
use std::{
    net::{IpAddr, Ipv4Addr},
    process::Stdio,
    time::Duration,
};
use tokio::{io::AsyncReadExt, process::Command, time::timeout};

const MAX_REPORT: u64 = 128 * 1024;
const READ_DEADLINE: Duration = Duration::from_secs(2);
const PER_FAMILY: usize = 2;

pub use selected::time_servers;

async fn read_status(mut command: Command) -> Option<Vec<u8>> {
    let mut child = command
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .stdout(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .ok()?;
    let mut output = child.stdout.take()?.take(MAX_REPORT + 1);
    let mut bytes = Vec::new();
    let completed = timeout(READ_DEADLINE, async {
        output.read_to_end(&mut bytes).await?;
        if bytes.len() as u64 > MAX_REPORT {
            return Ok::<_, std::io::Error>(false);
        }
        Ok(child.wait().await?.success())
    })
    .await;
    match completed {
        Ok(Ok(true)) => Some(bytes),
        _ => {
            let _ = child.kill().await;
            None
        }
    }
}

#[cfg(target_os = "linux")]
mod selected {
    use super::*;
    use serde::Deserialize;

    #[derive(Deserialize)]
    #[serde(rename_all = "PascalCase")]
    struct Link {
        index: u32,
        #[serde(default)]
        addresses: Vec<Address>,
        #[serde(rename = "NTP", default)]
        ntp: Vec<Address>,
    }

    #[derive(Deserialize)]
    #[serde(rename_all = "PascalCase")]
    struct Address {
        #[serde(default)]
        family: i32,
        #[serde(default)]
        address: Vec<u8>,
        config_source: Option<String>,
    }

    impl Address {
        fn ip(&self) -> Option<IpAddr> {
            match self.family {
                libc::AF_INET => <[u8; 4]>::try_from(self.address.as_slice())
                    .ok()
                    .map(IpAddr::from),
                libc::AF_INET6 => <[u8; 16]>::try_from(self.address.as_slice())
                    .ok()
                    .map(IpAddr::from),
                _ => None,
            }
        }
    }

    pub async fn time_servers(index: u32, local: Ipv4Addr) -> Vec<IpAddr> {
        let mut command = Command::new("/usr/bin/networkctl");
        command.args(["--json=short", "--no-pager", "status", &index.to_string()]);
        read_status(command)
            .await
            .map_or_else(Vec::new, |report| parse(&report, index, local))
    }

    fn parse(report: &[u8], index: u32, local: Ipv4Addr) -> Vec<IpAddr> {
        let Ok(link) = serde_json::from_slice::<Link>(report) else {
            return Vec::new();
        };
        if link.index != index || !link.addresses.iter().any(|a| a.ip() == Some(local.into())) {
            return Vec::new();
        }
        let mut servers = Vec::new();
        for (family, source) in [(libc::AF_INET6, "DHCPv6"), (libc::AF_INET, "DHCPv4")] {
            let mut candidates = link
                .ntp
                .iter()
                .filter(|a| a.family == family && a.config_source.as_deref() == Some(source))
                .filter_map(Address::ip)
                .filter(|address| {
                    !address.is_unspecified()
                        && !address.is_multicast()
                        && !address.is_loopback()
                        && *address != IpAddr::V4(Ipv4Addr::BROADCAST)
                });
            for _ in 0..PER_FAMILY {
                if let Some(address) = candidates.find(|address| !servers.contains(address)) {
                    servers.push(address);
                }
            }
        }
        servers
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn checks_link_and_preserves_dhcp_priority() {
            let report = br#"{
                "Index":7,"Addresses":[{"Family":2,"Address":[192,0,2,1]}],
                "NTP":[{"Family":2,"Address":[192,0,2,2],"ConfigSource":"runtime"},
                       {"Family":2,"Address":[192,0,2,3],"ConfigSource":"DHCPv4"},
                       {"Server":"clock.example","ConfigSource":"DHCPv6"},
                       {"Family":10,"Address":[254,128,0,0,0,0,0,0,0,0,0,0,0,0,0,4],"ConfigSource":"DHCPv6"},
                       {"Family":2,"Address":[224,0,1,1],"ConfigSource":"DHCPv4"}]
            }"#;
            let local = Ipv4Addr::new(192, 0, 2, 1);
            assert_eq!(
                parse(report, 7, local),
                [
                    "fe80::4".parse::<IpAddr>().unwrap(),
                    Ipv4Addr::new(192, 0, 2, 3).into()
                ]
            );
            assert!(parse(report, 8, local).is_empty());
            assert!(parse(report, 7, Ipv4Addr::new(192, 0, 2, 99)).is_empty());
        }
    }
}

#[cfg(target_os = "macos")]
mod selected {
    use super::*;

    pub async fn time_servers(index: u32, local: Ipv4Addr) -> Vec<IpAddr> {
        let Some(interface) = if_addrs::get_if_addrs().ok().and_then(|interfaces| {
            interfaces
                .into_iter()
                .find(|i| i.index == Some(index) && i.ip() == local)
        }) else {
            return Vec::new();
        };
        let mut command = Command::new("/usr/sbin/ipconfig");
        command.args(["getoption", &interface.name, "42"]);
        read_status(command)
            .await
            .and_then(|bytes| String::from_utf8(bytes).ok())
            .map_or_else(Vec::new, |report| {
                report
                    .split_ascii_whitespace()
                    .filter_map(|item| item.parse::<Ipv4Addr>().ok())
                    .filter(|a| {
                        !a.is_unspecified()
                            && !a.is_multicast()
                            && !a.is_loopback()
                            && !a.is_broadcast()
                    })
                    .map(IpAddr::from)
                    .take(PER_FAMILY)
                    .collect()
            })
    }
}
