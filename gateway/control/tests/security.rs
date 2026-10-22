#![cfg(unix)]
use nxrs_gateway_control::{config::Config, security::{Auth, GatewayPolicy, RemoteSession, Settings}};
use axum::http::StatusCode;
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{fs, os::unix::fs::PermissionsExt, path::PathBuf, time::{Duration, Instant}};

fn digest(s: &str) -> String { Sha256::digest(s.as_bytes()).iter().map(|b| format!("{b:02x}")).collect() }
fn id(n: u64) -> String { format!("00000000-0000-4000-8000-{n:012x}") }
struct Fixture { directory: PathBuf, config: Config, settings: Settings }
impl Fixture {
    fn new() -> Self {
        let mut random = [0u8; 16]; getrandom::fill(&mut random).unwrap();
        let suffix: String = random.iter().map(|b| format!("{b:02x}")).collect();
        let directory = std::env::temp_dir().join(format!("nxrs-policy-{suffix}"));
        fs::create_dir(&directory).unwrap(); fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
        let path = directory.join("revocations.json");
        fs::write(&path, b"{\"version\":1,\"revoked\":[]}").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        let config = serde_json::from_value(json!({
            "listen":"127.0.0.1:18080","auth_listen":"127.0.0.1:18081",
            "grant_ttl_seconds":60,"max_grants":64,"media_origins":["https://media.test"],
            "cameras":[{"camera_id":"cam_one","gateway_id":"gw_one","site_id":"site_one","name":"Synthetic",
                "source":"synthetic","stream_path":"one","whep_endpoint":"https://media.test/one/whep"}],
            "viewers":[
                {"viewer_id":"viewer_a","site_id":"site_one","camera_ids":["cam_one"],"token_sha256":digest(&"a".repeat(64))},
                {"viewer_id":"viewer_b","site_id":"site_one","camera_ids":["cam_one"],"token_sha256":digest(&"b".repeat(64))}],
            "publishers":[{"username":"publisher","camera_id":"cam_one","token_sha256":digest(&"c".repeat(64))}]
        })).unwrap();
        Self { directory, config, settings: Settings { revocations_file:path, media_api:"127.0.0.1:19997".parse().unwrap(), media_api_token:"d".repeat(64) } }
    }
    fn policy(&self) -> GatewayPolicy { GatewayPolicy::new(self.config.clone(), Some(self.settings.clone())).unwrap() }
}
impl Drop for Fixture { fn drop(&mut self) { let _ = fs::remove_dir_all(&self.directory); } }
fn auth(token: &str, number: Option<u64>) -> Auth {
    serde_json::from_value(json!({"action":"read","protocol":"webrtc","path":"one","token":token,
        "id":number.map(id)})).unwrap()
}
fn token(policy: &mut GatewayPolicy, account: &str, now: Instant) -> String {
    policy.issue(account,"cam_one",1000,now).unwrap()["access_token"].as_str().unwrap().to_owned()
}
fn remote(n: u64, established: bool) -> RemoteSession {
    RemoteSession { id:id(n),state:"read".into(),path:"one".into(),established }
}

#[test] fn recording_management_is_explicit_and_revocation_covers_history() {
    let mut f = Fixture::new(); f.config.viewers[0].manage_recordings = true;
    let mut p = f.policy();
    assert_eq!(p.activity_scope(&"a".repeat(64), "cam_one", true), Ok(true));
    assert_eq!(p.activity_scope(&"b".repeat(64), "cam_one", false), Ok(false));
    assert_eq!(p.activity_scope(&"b".repeat(64), "cam_one", true), Err(StatusCode::FORBIDDEN));
    assert_eq!(p.activity_scope(&"a".repeat(64), "unlisted", false), Err(StatusCode::NOT_FOUND));
    assert_eq!(p.activity_scope(&"c".repeat(64), "cam_one", false), Err(StatusCode::UNAUTHORIZED));
    p.revoke("viewer_a").unwrap();
    assert_eq!(p.activity_scope(&"a".repeat(64), "cam_one", false), Err(StatusCode::FORBIDDEN));
    drop(p);
    assert_eq!(f.policy().activity_scope(&"a".repeat(64), "cam_one", false), Err(StatusCode::FORBIDDEN));
}

#[test] fn recorder_credentials_only_read_their_rtsp_path_and_cannot_be_management_keys() {
    let mut f = Fixture::new();
    f.config.activity = Some(nxrs_camera_activity::store::Settings { directory:f.directory.join("activity"),
        camera_ids:vec!["cam_one".into()], retention_seconds:86400, max_bytes:64*1024*1024, max_events:100 });
    f.config.recorders.push(nxrs_gateway_control::config::Recorder { username:"recorder".into(),
        camera_id:"cam_one".into(), token_sha256:digest(&"e".repeat(64)) });
    let mut p = f.policy(); let now = Instant::now();
    let request = |action: &str, path: &str, protocol: &str| serde_json::from_value(json!({
        "user":"recorder", "password":"e".repeat(64), "token":"e".repeat(64),
        "action":action, "path":path, "protocol":protocol })).unwrap();
    assert!(p.authorize_media(&request("read", "one", "rtsp"), 1000, now));
    for (action, path, protocol) in [("publish","one","rtsp"),("read","other","rtsp"),
        ("read","one","webrtc"),("api","",""),("playback","one","rtsp")] {
        assert!(!p.authorize_media(&request(action,path,protocol),1000,now));
    }
    let mut settings = f.settings.clone(); settings.media_api_token = "e".repeat(64);
    assert!(GatewayPolicy::new(f.config.clone(),Some(settings)).is_err());
}

#[test] fn new_admission_requires_fresh_control_and_a_real_session_identifier() {
    let fixture = Fixture::new(); let mut p = fixture.policy(); let now = Instant::now();
    assert_eq!(p.issue(&"a".repeat(64),"cam_one",1000,now).unwrap_err(),StatusCode::SERVICE_UNAVAILABLE);
    p.synchronized(true,now); let grant = token(&mut p,&"a".repeat(64),now);
    assert!(!p.authorize_media(&auth(&grant,None),1000,now));
    assert!(p.authorize_media(&auth(&grant,Some(1)),1000,now));
    assert!(!p.authorize_media(&auth(&grant,Some(2)),1000,now+Duration::from_secs(6)));
    p.synchronized(false,now); assert!(!p.authorize_media(&auth(&grant,Some(2)),1000,now));
}
#[test] fn per_viewer_slots_and_duplicate_callbacks_preserve_ownership() {
    let fixture=Fixture::new(); let mut p=fixture.policy(); let now=Instant::now(); p.synchronized(true,now);
    let a=token(&mut p,&"a".repeat(64),now); let b=token(&mut p,&"b".repeat(64),now);
    for n in 1..=4 { assert!(p.authorize_media(&auth(&a,Some(n)),1000,now)); }
    assert!(p.authorize_media(&auth(&a,Some(1)),1000,now));
    assert!(!p.authorize_media(&auth(&a,Some(5)),1000,now));
    assert!(!p.authorize_media(&auth(&b,Some(1)),1000,now));
    assert!(p.authorize_media(&auth(&b,Some(5)),1000,now));
}
#[test] fn revoke_is_persistent_and_does_not_select_other_viewers() {
    let fixture=Fixture::new(); let mut p=fixture.policy(); let now=Instant::now(); p.synchronized(true,now);
    let a=token(&mut p,&"a".repeat(64),now); let b=token(&mut p,&"b".repeat(64),now);
    assert!(p.authorize_media(&auth(&a,Some(1)),1000,now));
    assert!(p.authorize_media(&auth(&b,Some(2)),1000,now));
    assert_eq!(p.revoke("viewer_a").unwrap(),StatusCode::ACCEPTED);
    assert_eq!(p.plan(&[remote(1,true),remote(2,true)],now),vec![id(1)]);
    assert_eq!(p.catalog(&"a".repeat(64)).unwrap_err(),StatusCode::FORBIDDEN);
    assert!(p.catalog(&"b".repeat(64)).is_ok());
    assert!(!p.authorize_media(&auth(&a,Some(3)),1000,now));
    drop(p); let p=fixture.policy();
    assert_eq!(p.catalog(&"a".repeat(64)).unwrap_err(),StatusCode::FORBIDDEN);
    assert!(p.catalog(&"b".repeat(64)).is_ok());
}
#[test] fn snapshot_race_cannot_kick_a_newly_owned_session() {
    let fixture=Fixture::new(); let mut p=fixture.policy(); let now=Instant::now(); p.synchronized(true,now);
    let a=token(&mut p,&"a".repeat(64),now);
    assert_eq!(p.plan(&[remote(1,true)],now),vec![id(1)]);
    assert!(p.authorize_media(&auth(&a,Some(1)),1000,now));
    assert!(!p.still_forbidden(&id(1)));
    p.revoke("viewer_a").unwrap(); assert!(p.still_forbidden(&id(1)));
    p.retired(&id(1)); assert_eq!(p.revocation_status("viewer_a",now).unwrap()["tracked_sessions"],0);
}
#[test] fn admission_expiry_is_not_an_implicit_viewing_time_limit() {
    let fixture=Fixture::new(); let mut p=fixture.policy(); let now=Instant::now(); p.synchronized(true,now);
    let a=token(&mut p,&"a".repeat(64),now); assert!(p.authorize_media(&auth(&a,Some(1)),1000,now));
    let later=now+Duration::from_secs(61); p.synchronized(true,later);
    assert!(!p.authorize_media(&auth(&a,Some(2)),1061,later));
    assert!(p.plan(&[remote(1,true)],later).is_empty());
    p.revoke("viewer_a").unwrap(); assert_eq!(p.plan(&[remote(1,true)],later),vec![id(1)]);
}
#[test] fn restart_retires_orphans_but_not_publishers_or_fresh_negotiation() {
    let fixture=Fixture::new(); let mut p=fixture.policy(); let now=Instant::now();
    let mut publisher=remote(2,true); publisher.state="publish".into();
    assert_eq!(p.plan(&[remote(1,false),publisher],now),vec![id(1)]);
    p.synchronized(true,now);
    assert!(p.plan(&[remote(3,false)],now).is_empty());
    assert_eq!(p.plan(&[remote(3,true)],now),vec![id(3)]);
}
#[test] fn failed_persistence_is_an_error_not_successful_revocation() {
    let fixture=Fixture::new(); let mut p=fixture.policy(); let now=Instant::now(); p.synchronized(true,now);
    let backup=fixture.directory.join("backup.json"); fs::rename(&fixture.settings.revocations_file,&backup).unwrap();
    assert_eq!(p.revoke("viewer_a").unwrap_err(),StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(p.catalog(&"a".repeat(64)).unwrap_err(),StatusCode::FORBIDDEN);
    assert_eq!(p.issue(&"b".repeat(64),"cam_one",1000,now).unwrap_err(),StatusCode::SERVICE_UNAVAILABLE);
    fs::rename(&backup,&fixture.settings.revocations_file).unwrap();
    assert_eq!(p.revoke("viewer_a").unwrap(),StatusCode::ACCEPTED);
    drop(p); assert_eq!(fixture.policy().catalog(&"a".repeat(64)).unwrap_err(),StatusCode::FORBIDDEN);
}
#[test] fn management_credentials_are_separate_and_legacy_mode_remains_explicit() {
    let fixture=Fixture::new(); let now=Instant::now();
    for address in ["0.0.0.0:9997","192.0.2.1:9997","127.0.0.1:18080"] {
        let mut settings=fixture.settings.clone(); settings.media_api=address.parse().unwrap();
        assert!(GatewayPolicy::new(fixture.config.clone(),Some(settings)).is_err());
    }
    let mut settings=fixture.settings.clone(); settings.media_api_token="a".repeat(64);
    assert!(GatewayPolicy::new(fixture.config.clone(),Some(settings)).is_err());
    let mut p=fixture.policy();
    for (credential,expected) in [("d",true),("a",false),("b",false),("c",false)] {
        let request=serde_json::from_value(json!({"action":"api","protocol":"","path":"","token":credential.repeat(64),"query":"itemsPerPage=256"})).unwrap();
        assert_eq!(p.authorize_media(&request,1000,now),expected);
    }
    drop(p);
    let mut legacy=GatewayPolicy::new(fixture.config.clone(),None).unwrap();
    let a=token(&mut legacy,&"a".repeat(64),now);
    assert!(legacy.authorize_media(&auth(&a,None),1000,now));
    assert_eq!(legacy.revoke("viewer_a").unwrap(),StatusCode::NO_CONTENT);
}
#[test] fn closed_observed_sessions_free_slots_without_discarding_racing_admissions() {
    let fixture=Fixture::new(); let mut p=fixture.policy(); let now=Instant::now(); p.synchronized(true,now);
    let a=token(&mut p,&"a".repeat(64),now);
    for n in 1..=4 { assert!(p.authorize_media(&auth(&a,Some(n)),1000,now)); }
    // An old empty list must not discard newly admitted ownership.
    assert!(p.plan(&[],now).is_empty());
    assert_eq!(p.revocation_status("viewer_a",now).unwrap()["tracked_sessions"],4);
    assert!(p.plan(&[remote(1,true),remote(2,true),remote(3,true),remote(4,true)],now).is_empty());
    assert!(p.plan(&[remote(2,true),remote(3,true),remote(4,true)],now).is_empty());
    assert!(p.authorize_media(&auth(&a,Some(5)),1000,now));
    assert!(!p.authorize_media(&auth(&a,Some(6)),1000,now));
}
#[test] fn remote_path_mismatch_remains_denied_at_final_kick_recheck() {
    let fixture=Fixture::new(); let mut p=fixture.policy(); let now=Instant::now(); p.synchronized(true,now);
    let a=token(&mut p,&"a".repeat(64),now); assert!(p.authorize_media(&auth(&a,Some(1)),1000,now));
    let mut mismatch=remote(1,true); mismatch.path="other".into();
    assert_eq!(p.plan(&[mismatch],now),vec![id(1)]);
    assert!(p.still_forbidden(&id(1)));
    assert!(!p.authorize_media(&auth(&a,Some(1)),1000,now));
}
