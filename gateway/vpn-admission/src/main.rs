//! Read-only WireGuard configuration/history admission before interface startup.
#![forbid(unsafe_code)]
use serde::{Deserialize, Deserializer};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    net::Ipv4Addr,
    path::{Component, Path, PathBuf},
};
use zeroize::Zeroizing;

type Result<T> = std::result::Result<T, &'static str>;
const MAX_RETIRED: usize = 4096;
const DEFAULT_FIREWALL: &str = "/etc/nxrs-monitor/firewall.nft";

fn require(ok: bool, category: &'static str) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(category)
    }
}

// Retain field presence: missing is distinct from an explicitly null active peer.
#[derive(Default)]
enum Field<T> {
    #[default]
    Missing,
    Present(T),
}
impl<'de, T: Deserialize<'de>> Deserialize<'de> for Field<T> {
    fn deserialize<D: Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
        T::deserialize(d).map(Self::Present)
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct History {
    version: u64,
    generation: u64,
    gateway_public_key: String,
    #[serde(default)]
    active_peer: Field<Option<String>>,
    retired_peers: Vec<String>,
    config_sha256: String,
    #[serde(default)]
    peer_address: Field<Option<String>>,
}

fn key(text: &str) -> Result<()> {
    // A canonical padded Base64 representation of exactly 32 nonzero bytes.
    // Validate encoding only; WireGuard owns its crypto and peer authentication.
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let b = text.as_bytes();
    require(b.len() == 44 && b[43] == b'=', "key encoding")?;
    require(
        b[..42].iter().all(|c| ALPHABET.contains(c))
            && ALPHABET
                .iter()
                .position(|c| *c == b[42])
                .is_some_and(|i| i % 4 == 0)
            && b[..43].iter().any(|c| *c != b'A'),
        "key encoding",
    )
}
fn address(text: &str) -> Result<Ipv4Addr> {
    let ip = text.parse::<Ipv4Addr>().map_err(|_| "tunnel address")?;
    require(ip.is_private() && ip.to_string() == text, "tunnel address")?;
    Ok(ip)
}
fn safe_path(path: &Path) -> Result<&str> {
    let text = path.to_str().ok_or("resource path")?;
    require(
        path.is_absolute()
            && !path.components().any(|p| matches!(p, Component::ParentDir))
            && text
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"/_.-".contains(&b)),
        "resource path",
    )?;
    Ok(text)
}
struct Configuration<'a> {
    gateway: Ipv4Addr,
    peer: Option<Ipv4Addr>,
    peer_key: Option<&'a str>,
}
fn configuration<'a>(raw: &'a [u8], firewall: &Path) -> Result<Configuration<'a>> {
    require(!raw.is_empty() && raw.len() <= 8192, "configuration bound")?;
    let text = std::str::from_utf8(raw).map_err(|_| "configuration encoding")?;
    let field = |name: &str| -> Result<&'a str> {
        let prefix = format!("{name} = ");
        let mut values = text.lines().filter_map(|line| line.strip_prefix(&prefix));
        let value = values.next().ok_or("configuration field")?;
        require(values.next().is_none(), "configuration field")?;
        Ok(value)
    };
    let gateway = address(field("Address")?.strip_suffix("/32").ok_or("host route")?)?;
    let secret = field("PrivateKey")?;
    key(secret)?;
    let mut expected=Zeroizing::new(format!("[Interface]\nAddress = {gateway}/32\nListenPort = 51820\nMTU = 1380\nPreUp = /usr/sbin/nft -f {}\nPrivateKey = {secret}\n",safe_path(firewall)?));
    let (peer, peer_key) = if text.contains("\n[Peer]\n") {
        let peer = address(
            field("AllowedIPs")?
                .strip_suffix("/32")
                .ok_or("host route")?,
        )?;
        require(peer != gateway, "distinct addresses")?;
        let public = field("PublicKey")?;
        key(public)?;
        expected.push_str(&format!(
            "\n[Peer]\nPublicKey = {public}\nAllowedIPs = {peer}/32\n"
        ));
        (Some(peer), Some(public))
    } else {
        (None, None)
    };
    require(
        expected.as_bytes() == raw,
        "canonical reference configuration",
    )?;
    Ok(Configuration {
        gateway,
        peer,
        peer_key,
    })
}
fn validate(raw: &[u8], ledger: &[u8], firewall: &Path) -> Result<()> {
    require(
        !ledger.is_empty() && ledger.len() <= 1024 * 1024,
        "history bound",
    )?;
    let c = configuration(raw, firewall)?;
    let h: History = serde_json::from_slice(ledger).map_err(|_| "history schema")?;
    let active = match &h.active_peer {
        Field::Present(v) => v.as_deref(),
        Field::Missing => return Err("active peer field"),
    };
    require(
        matches!(h.version, 1 | 2)
            && h.retired_peers.len() + usize::from(active.is_some()) <= MAX_RETIRED + 1,
        "history version or capacity",
    )?;
    key(&h.gateway_public_key)?;
    if let Some(active) = active {
        key(active)?;
    }
    let mut unique = BTreeSet::new();
    for retired in &h.retired_peers {
        key(retired)?;
        require(
            unique.insert(retired.as_str())
                && retired != &h.gateway_public_key
                && Some(retired.as_str()) != active,
            "retired key history",
        )?;
    }
    require(
        active != Some(h.gateway_public_key.as_str()) && active == c.peer_key,
        "matching active peer",
    )?;
    let retired = h.retired_peers.len() as u64;
    if h.version == 1 {
        require(
            active.is_some()
                && matches!(h.peer_address, Field::Missing)
                && h.generation == retired + 1,
            "version1 history",
        )?;
    } else {
        let peer = match &h.peer_address {
            Field::Present(Some(v)) => address(v)?,
            _ => return Err("retained peer address"),
        };
        require(
            peer != c.gateway
                && c.peer.is_none_or(|p| p == peer)
                && retired < h.generation
                && h.generation <= 2 * retired + u64::from(active.is_some()),
            "version2 history",
        )?;
    }
    require(
        h.config_sha256.len() == 64
            && h.config_sha256
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            && h.config_sha256 == format!("{:x}", Sha256::digest(raw)),
        "matching configuration digest",
    )
}
#[cfg(unix)]
fn private_parent(path: &Path, owner: u32) -> Result<()> {
    use std::os::unix::fs::MetadataExt;
    let parent = path.parent().ok_or("private parent")?;
    let m = std::fs::symlink_metadata(parent).map_err(|_| "private parent")?;
    require(
        m.is_dir() && m.uid() == owner && m.mode() & 0o077 == 0,
        "private parent",
    )
}
#[cfg(not(unix))]
fn private_parent(_: &Path, _: u32) -> Result<()> {
    Err("Unix required")
}
fn admit(config: &Path, history: &Path, firewall: &Path, owner: u32) -> Result<()> {
    private_parent(config, owner)?;
    private_parent(history, owner)?;
    let raw = Zeroizing::new(nxrs_gateway_private_file::read_owned(config, 8192, owner)?);
    let ledger = nxrs_gateway_private_file::read_owned(history, 1024 * 1024, owner)?;
    validate(&raw, &ledger, firewall)?;
    // Privileged configuration writers must serialize maintenance. Recheck both
    // files; this does not pin paths through the later wg-quick invocation.
    require(
        raw.as_slice()
            == Zeroizing::new(nxrs_gateway_private_file::read_owned(config, 8192, owner)?)
                .as_slice()
            && ledger == nxrs_gateway_private_file::read_owned(history, 1024 * 1024, owner)?,
        "files changed during admission",
    )?;
    private_parent(config, owner)?;
    private_parent(history, owner)
}
fn run() -> Result<()> {
    let mut args = std::env::args_os().skip(1);
    let mut config = None;
    let mut history = None;
    let mut firewall = None;
    while let Some(flag) = args.next() {
        let value = PathBuf::from(args.next().ok_or("arguments")?);
        match flag.to_str() {
            Some("--config") if config.is_none() => config = Some(value),
            Some("--history") if history.is_none() => history = Some(value),
            Some("--firewall") if firewall.is_none() => firewall = Some(value),
            _ => return Err("arguments"),
        }
    }
    admit(
        &config.ok_or("arguments")?,
        &history.ok_or("arguments")?,
        &firewall.unwrap_or_else(|| PathBuf::from(DEFAULT_FIREWALL)),
        0,
    )
}
fn main() {
    if run().is_err() {
        eprintln!("VPN configuration/history admission refused.");
        std::process::exit(1);
    }
    println!("VPN configuration/history admitted.");
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};
    fn k(first: char) -> String {
        format!("{first}{}=", "A".repeat(42))
    }
    fn config(active: bool) -> Vec<u8> {
        let mut value=format!("[Interface]\nAddress = 10.77.0.1/32\nListenPort = 51820\nMTU = 1380\nPreUp = /usr/sbin/nft -f {DEFAULT_FIREWALL}\nPrivateKey = {}\n",k('B'));
        if active {
            value.push_str(&format!(
                "\n[Peer]\nPublicKey = {}\nAllowedIPs = 10.77.0.2/32\n",
                k('D')
            ));
        }
        value.into_bytes()
    }
    fn history(raw: &[u8], version: u64, active: bool) -> Value {
        let mut h = json!({"version":version,"generation":1,"gateway_public_key":k('C'),"active_peer":k('D'),
            "retired_peers":[],"config_sha256":format!("{:x}",Sha256::digest(raw))});
        if version == 2 {
            h["peer_address"] = json!("10.77.0.2");
            h["retired_peers"] = json!([k('E')]);
            h["generation"] = json!(if active { 3 } else { 2 });
            if !active {
                h["active_peer"] = Value::Null;
            }
        }
        h
    }
    fn accepts(raw: &[u8], h: &Value) -> bool {
        validate(
            raw,
            &serde_json::to_vec(h).unwrap(),
            Path::new(DEFAULT_FIREWALL),
        )
        .is_ok()
    }
    #[test]
    fn canonical_active_retired_and_recovered_pairs_admit() {
        for (version, active) in [(1, true), (2, false), (2, true)] {
            let raw = config(active);
            assert!(accepts(&raw, &history(&raw, version, active)));
        }
    }
    #[test]
    fn missing_null_duplicate_unknown_and_wrong_typed_fields_refuse() {
        let raw = config(true);
        let original = history(&raw, 1, true);
        for name in [
            "version",
            "generation",
            "gateway_public_key",
            "active_peer",
            "retired_peers",
            "config_sha256",
        ] {
            let mut h = original.clone();
            h.as_object_mut().unwrap().remove(name);
            assert!(!accepts(&raw, &h), "missing {name}");
        }
        for (name, value) in [
            ("version", json!(true)),
            ("generation", json!(true)),
            ("generation", json!(-1)),
            ("active_peer", Value::Null),
            ("peer_address", Value::Null),
            ("unknown", json!(true)),
        ] {
            let mut h = original.clone();
            h[name] = value;
            assert!(!accepts(&raw, &h), "invalid {name}");
        }
        let original = serde_json::to_string(&original).unwrap();
        let duplicate = format!("{{\"version\":1,{}", &original[1..]);
        assert!(validate(&raw, duplicate.as_bytes(), Path::new(DEFAULT_FIREWALL)).is_err());
    }
    #[test]
    fn every_retired_key_and_gateway_identity_remain_excluded() {
        let raw = config(true);
        let original = history(&raw, 2, true);
        for (name, value) in [
            ("active_peer", json!(k('E'))),
            ("gateway_public_key", json!(k('D'))),
            ("retired_peers", json!([k('E'), k('E')])),
            ("retired_peers", json!([k('C')])),
            ("peer_address", json!("10.77.0.3")),
            ("generation", json!(4)),
            ("config_sha256", json!("0".repeat(64))),
        ] {
            let mut h = original.clone();
            h[name] = value;
            assert!(!accepts(&raw, &h), "invalid {name}");
        }
        let raw = config(false);
        let mut h = history(&raw, 2, false);
        h["version"] = json!(1);
        assert!(!accepts(&raw, &h));
        let mut h = history(&raw, 2, false);
        h["peer_address"] = json!("10.77.0.1");
        assert!(!accepts(&raw, &h));
    }
    #[test]
    fn coherent_digest_cannot_hide_foreign_hooks_extra_peers_or_routes() {
        let original = String::from_utf8(config(true)).unwrap();
        for raw in [
            original.replace("MTU = 1380", "MTU = 1500"),
            original.replace("/32", "/24"),
            original.clone() + "PostUp = unexpected\n",
            original.clone() + "[Peer]\n",
            original.replace("10.77.0.2", "8.8.8.8"),
            original.replace("\n", "\r\n"),
            original + "PrivateKey = invalid\n",
        ] {
            assert!(!accepts(raw.as_bytes(), &history(raw.as_bytes(), 1, true)));
        }
    }
    #[test]
    fn explicit_resource_relocation_preserves_command_shape() {
        let relocated = Path::new("/private/fixture/firewall.nft");
        let raw = String::from_utf8(config(true))
            .unwrap()
            .replace(DEFAULT_FIREWALL, relocated.to_str().unwrap());
        let h = serde_json::to_vec(&history(raw.as_bytes(), 1, true)).unwrap();
        assert!(validate(raw.as_bytes(), &h, relocated).is_ok());
        assert!(validate(raw.as_bytes(), &h, Path::new(DEFAULT_FIREWALL)).is_err());
        for invalid in [
            "relative",
            "/private/../elsewhere",
            "/private/file;command",
            "/private/file\ncommand",
        ] {
            assert!(safe_path(Path::new(invalid)).is_err());
        }
    }
    #[test]
    fn full_legacy_history_can_retire_but_cannot_reopen_admission_beyond_capacity() {
        const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        let retired: Vec<String> = (0..MAX_RETIRED)
            .map(|n| {
                let mut key = vec![b'A'; 44];
                key[0] = b'E';
                key[1] = ALPHABET[n / 64];
                key[2] = ALPHABET[n % 64];
                key[43] = b'=';
                String::from_utf8(key).unwrap()
            })
            .collect();
        let active = config(true);
        let mut h = history(&active, 1, true);
        h["retired_peers"] = json!(retired);
        h["generation"] = json!(MAX_RETIRED + 1);
        assert!(accepts(&active, &h));
        let inactive = config(false);
        let mut h = history(&inactive, 2, false);
        let mut retired = retired;
        retired.push(k('F'));
        h["retired_peers"] = json!(retired);
        h["generation"] = json!(MAX_RETIRED + 2);
        assert!(accepts(&inactive, &h));
        h["active_peer"] = json!(k('D'));
        h["config_sha256"] = json!(format!("{:x}", Sha256::digest(&active)));
        assert!(!accepts(&active, &h));
    }
    #[test]
    fn canonical_base64_32_byte_key_encoding_is_required() {
        assert!(key(&k('B')).is_ok());
        for invalid in [
            "A".repeat(43) + "=",
            k('B').replace('=', ""),
            "B".repeat(43) + "=",
            "!".repeat(43) + "=",
        ] {
            assert!(key(&invalid).is_err());
        }
    }
    #[cfg(unix)]
    #[test]
    fn private_owned_files_admit_without_mutation_and_missing_or_public_files_refuse() {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        let root = std::env::temp_dir().join(format!(
            "vpn-startup-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&root).unwrap();
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700)).unwrap();
        let root = root.canonicalize().unwrap();
        let c = root.join("wg-nxrs.conf");
        let h = root.join("vpn-peers.json");
        let raw = config(true);
        let ledger = serde_json::to_vec(&history(&raw, 1, true)).unwrap();
        for (path, bytes) in [(&c, &raw), (&h, &ledger)] {
            std::fs::write(path, bytes).unwrap();
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)).unwrap();
        }
        let owner = std::fs::metadata(&c).unwrap().uid();
        assert!(admit(&c, &h, Path::new(DEFAULT_FIREWALL), owner).is_ok());
        assert!(admit(&c, &h, Path::new(DEFAULT_FIREWALL), owner.wrapping_add(1)).is_err());
        assert_eq!(std::fs::read(&c).unwrap(), raw);
        assert_eq!(std::fs::read(&h).unwrap(), ledger);
        std::fs::set_permissions(&h, std::fs::Permissions::from_mode(0o644)).unwrap();
        assert!(admit(&c, &h, Path::new(DEFAULT_FIREWALL), owner).is_err());
        std::fs::remove_file(&h).unwrap();
        assert!(admit(&c, &h, Path::new(DEFAULT_FIREWALL), owner).is_err());
        assert!(!h.exists());
        std::fs::remove_dir_all(root).unwrap();
    }
}

#[cfg(test)]
mod shared_contract {
    use super::*;
    #[derive(Deserialize)]
    struct Case {
        name: String,
        config: String,
        history: String,
        valid: bool,
    }
    #[test]
    fn actual_python_staging_and_rust_admission_share_one_bounded_contract() {
        let root = std::env::temp_dir().join(format!(
            "vpn-contract-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&root).unwrap();
        let output = root.join("corpus.json");
        let script = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .join("tests/vpn_admission_contract.py");
        let status = std::process::Command::new("python3")
            .arg("-B")
            .arg(script)
            .arg("--output")
            .arg(&output)
            .status()
            .unwrap();
        assert!(status.success());
        let raw = std::fs::read(&output).unwrap();
        assert!(raw.len() < 65536);
        let cases: Vec<Case> = serde_json::from_slice(&raw).unwrap();
        assert_eq!(cases.len(), 32);
        for case in cases {
            assert_eq!(
                validate(
                    case.config.as_bytes(),
                    case.history.as_bytes(),
                    Path::new(DEFAULT_FIREWALL)
                )
                .is_ok(),
                case.valid,
                "{}",
                case.name
            );
        }
        std::fs::remove_dir_all(root).unwrap();
    }
}
