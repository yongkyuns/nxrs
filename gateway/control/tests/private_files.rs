#![cfg(unix)]
use nxrs_gateway_control::{config::Config, security::{GatewayPolicy, Settings}};
use std::{fs::{self, OpenOptions}, os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt}, path::PathBuf};

struct Directory(PathBuf);
impl Drop for Directory { fn drop(&mut self) { let _ = fs::remove_dir_all(&self.0); } }

// A forked helper can briefly inherit another thread's lock before exec.
// Keep FIFO setup in its own test process, outside the parallel lock-owner suite.
#[test] fn fifo_read_returns_without_a_writer_and_cannot_be_a_state_lock() {
    let mut nonce = [0; 16]; getrandom::fill(&mut nonce).unwrap();
    let path = std::env::temp_dir().join(format!("nxrs-private-fifo-{:032x}", u128::from_ne_bytes(nonce)));
    fs::DirBuilder::new().mode(0o700).create(&path).unwrap();
    let directory = Directory(path);
    let state = directory.0.join("revocations.json");
    fs::write(&state, b"{\"version\":1,\"revoked\":[]}").unwrap();
    fs::set_permissions(&state, fs::Permissions::from_mode(0o600)).unwrap();
    let config: Config = serde_json::from_value(serde_json::json!({
        "listen":"127.0.0.1:18080", "auth_listen":"127.0.0.1:18081", "grant_ttl_seconds":60,
        "max_grants":64, "media_origins":["https://media.test"],
        "cameras":[{"camera_id":"cam_one", "gateway_id":"gw_one", "site_id":"site_one", "name":"Synthetic",
            "source":"synthetic", "stream_path":"one", "whep_endpoint":"https://media.test/one/whep"}],
        "viewers":[{"viewer_id":"viewer", "site_id":"site_one", "camera_ids":["cam_one"], "token_sha256":"a".repeat(64)}],
        "publishers":[]
    })).unwrap();
    let settings = Settings { revocations_file:state, media_api:"127.0.0.1:19997".parse().unwrap(), media_api_token:"b".repeat(64) };
    drop(GatewayPolicy::new(config.clone(), Some(settings.clone())).unwrap());
    let fifo = directory.0.join(".nxrs-revocations.lock");
    fs::remove_file(&fifo).unwrap();
    assert!(std::process::Command::new("mkfifo").args(["-m", "600"]).arg(&fifo).status().unwrap().success());
    let (reply, result) = std::sync::mpsc::sync_channel(1);
    let path = fifo.clone();
    let reader = std::thread::spawn(move || { let _ = reply.send(Settings::read(&path).is_err()); });
    let prompt = result.recv_timeout(std::time::Duration::from_secs(2));
    // Unblock an accidentally blocking open before joining, even on failure.
    let wake = OpenOptions::new().read(true).write(true).custom_flags(libc::O_NONBLOCK).open(&fifo).unwrap();
    drop(wake);
    reader.join().unwrap();
    assert_eq!(prompt, Ok(true));
    assert!(GatewayPolicy::new(config, Some(settings)).is_err());
}
