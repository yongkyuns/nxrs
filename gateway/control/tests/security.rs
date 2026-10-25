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
        let directory=fs::canonicalize(directory).unwrap();
        let path = directory.join("revocations.json");
        fs::write(&path, b"{\"version\":1,\"revoked\":[]}").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        let mut config: Config = serde_json::from_value(json!({
            "listen":"127.0.0.1:18080","auth_listen":"127.0.0.1:18081",
            "grant_ttl_seconds":60,"max_grants":64,"media_origins":["https://media.test"],
            "cameras":[{"camera_id":"cam_one","gateway_id":"gw_one","site_id":"site_one","name":"Synthetic",
                "source":"synthetic","stream_path":"one","whep_endpoint":"https://media.test/one/whep"}],
            "viewers":[
                {"viewer_id":"viewer_a","site_id":"site_one","camera_ids":["cam_one"],"token_sha256":digest(&"a".repeat(64))},
                {"viewer_id":"viewer_b","site_id":"site_one","camera_ids":["cam_one"],"token_sha256":digest(&"b".repeat(64))}],
            "publishers":[{"username":"publisher","camera_id":"cam_one","token_sha256":digest(&"c".repeat(64))}]
        })).unwrap();
        let wall=std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs();
        config.credential_window=Some(nxrs_gateway_control::credentials::Window { not_before:wall-1,expires_at:wall+3600 });
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
    f.config.activity = Some(nxrs_camera_activity::store::Settings { protection:None, directory:f.directory.join("activity"),
        camera_ids:vec!["cam_one".into()], retention_seconds:86400, max_bytes:64*1024*1024, max_events:100 });
    f.config.recorders.push(nxrs_gateway_control::config::Recorder { username:"recorder".into(),
        camera_id:"cam_one".into(), token_sha256:digest(&"e".repeat(64)) });
    let mut p = f.policy(); let now = Instant::now(); p.synchronized(true,now);
    let request = |action: &str, path: &str, protocol: &str| serde_json::from_value(json!({
        "user":"recorder", "password":"e".repeat(64), "token":"e".repeat(64),
        "action":action, "path":path, "protocol":protocol, "id":id(10) })).unwrap();
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

/// Actual loopback HTTP failures exercise the supervision future used by main.
/// The production five-second budget is retained in these tests.
struct OwnedTask(tokio::task::JoinHandle<()>);
impl Drop for OwnedTask { fn drop(&mut self) { self.0.abort(); } }

async fn management_fixture(mode: std::sync::Arc<std::sync::atomic::AtomicU8>) -> (Fixture, OwnedTask) {
    use axum::{extract::State, routing::{get, post}, Json, Router};
    use std::sync::{atomic::Ordering, Arc};
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let mut fixture = Fixture::new();
    fixture.settings.media_api = listener.local_addr().unwrap();
    let app = Router::new().route("/v3/webrtcsessions/list", get(
        |State(mode): State<Arc<std::sync::atomic::AtomicU8>>| async move {
            match mode.load(Ordering::SeqCst) {
                0 | 5..=9 => (StatusCode::OK, Json(json!({"itemCount":0,"pageCount":0,"items":[]}))),
                1 => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({}))),
                2 => (StatusCode::OK, Json(json!({"itemCount":1,"pageCount":0,"items":[]}))),
                3 => (StatusCode::OK, Json(json!({"itemCount":1,"pageCount":1,"items":[{
                    "id":id(1),"state":"read","path":"one","peerConnectionEstablished":true}]}))),
                _ => std::future::pending().await,
            }
        }))
        .route("/v3/rtspsessions/list", get(
            |State(mode): State<Arc<std::sync::atomic::AtomicU8>>| async move {
                match mode.load(Ordering::SeqCst) {
                    5 => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({}))),
                    6 => (StatusCode::OK, Json(json!({"itemCount":1,"pageCount":0,"items":[]}))),
                    7 => (StatusCode::OK, Json(json!({"itemCount":1,"pageCount":1,"items":[{
                        "id":id(1),"state":"publish","path":"one","user":"publisher"}]}))),
                    8 => std::future::pending().await,
                    9 => (StatusCode::OK, Json(json!({"itemCount":2,"pageCount":1,"items":[
                        {"id":id(1),"state":"publish","path":"one","user":"publisher"},
                        {"id":id(1),"state":"publish","path":"one","user":"publisher"}]}))),
                    _ => (StatusCode::OK, Json(json!({"itemCount":0,"pageCount":0,"items":[]}))),
                }
            }))
        .route("/v3/rtspsessions/kick/{id}", post(|| async { StatusCode::SERVICE_UNAVAILABLE }))
        .route("/v3/webrtcsessions/kick/{id}", post(|| async { StatusCode::SERVICE_UNAVAILABLE }))
        .with_state(mode);
    let task = OwnedTask(tokio::spawn(async move { axum::serve(listener, app).await.unwrap(); }));
    (fixture, task)
}

#[tokio::test]
async fn failed_management_status_schema_kick_and_hung_reply_retire_supervision() {
    use std::sync::{atomic::AtomicU8, Arc};
    let cases = [1,2,3,4,5,6,7,8,9].map(|mode| tokio::spawn(async move {
        let (fixture, _server) = management_fixture(Arc::new(AtomicU8::new(mode))).await;
        let (_, _, maintenance) = nxrs_gateway_control::managed_routers(
            fixture.config.clone(), Some(fixture.settings.clone())).unwrap();
        let begin = Instant::now();
        let failure=tokio::time::timeout(Duration::from_secs(7), maintenance.run()).await
            .expect("management failure must retire controller supervision");
        assert_eq!(failure,nxrs_gateway_control::SupervisionFailure::ManagementUnavailable);
        assert!(begin.elapsed() >= Duration::from_secs(5));
    }));
    for case in cases { case.await.unwrap(); }
}

#[tokio::test]
async fn complete_recovery_refreshes_health_but_later_failures_cannot_extend_it() {
    use std::sync::{atomic::{AtomicU8, Ordering}, Arc};
    let mode = Arc::new(AtomicU8::new(1));
    let (fixture, _server) = management_fixture(mode.clone()).await;
    let (_, _, maintenance) = nxrs_gateway_control::managed_routers(
        fixture.config.clone(), Some(fixture.settings.clone())).unwrap();
    let mut worker = tokio::spawn(maintenance.run());
    tokio::time::sleep(Duration::from_secs(2)).await;
    mode.store(0, Ordering::SeqCst);
    // The original startup deadline has passed; fresh complete reconciliation
    // must keep the same process healthy while the API remains available.
    assert!(tokio::time::timeout(Duration::from_secs(5), &mut worker).await.is_err());
    mode.store(1, Ordering::SeqCst);
    tokio::time::timeout(Duration::from_secs(7), &mut worker).await
        .expect("repeated failed reconciliations must not renew health").unwrap();
}


#[test]
fn strict_startup_requires_a_current_finite_credential_window() {
    let mut f=Fixture::new();
    f.config.credential_window=None;
    assert!(GatewayPolicy::new(f.config.clone(),Some(f.settings.clone())).is_err());
    assert!(GatewayPolicy::new(f.config.clone(),None).is_ok());
    let wall=std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs();
    for (begin,end) in [(wall-10,wall),(wall+10,wall+20),(wall,wall+90*86400+1)] {
        f.config.credential_window=Some(nxrs_gateway_control::credentials::Window { not_before:begin,expires_at:end });
        assert!(GatewayPolicy::new(f.config.clone(),Some(f.settings.clone())).is_err());
    }
}

#[tokio::test]
async fn credential_expiry_denies_every_application_role_and_retires_healthy_management() {
    use std::sync::{atomic::AtomicU8, Arc};
    let (mut f,_server)=management_fixture(Arc::new(AtomicU8::new(0))).await;
    let wall=std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs();
    f.config.credential_window=Some(nxrs_gateway_control::credentials::Window { not_before:wall-1,expires_at:wall+3 });
    f.config.activity=Some(nxrs_camera_activity::store::Settings { protection:None,directory:f.directory.join("activity"),
        camera_ids:vec!["cam_one".into()],retention_seconds:86400,max_bytes:64*1024*1024,max_events:100 });
    f.config.recorders.push(nxrs_gateway_control::config::Recorder { username:"recorder".into(),camera_id:"cam_one".into(),token_sha256:digest(&"e".repeat(64)) });
    let mut p=f.policy();let now=Instant::now();p.synchronized(true,now);
    let grant=token(&mut p,&"a".repeat(64),now);
    let roles:Vec<Auth>=vec![
        serde_json::from_value(json!({"action":"publish","protocol":"rtsp","path":"one","user":"publisher","password":"c".repeat(64),"token":"c".repeat(64),"id":id(20)})).unwrap(),
        serde_json::from_value(json!({"action":"read","protocol":"rtsp","path":"one","user":"recorder","password":"e".repeat(64),"token":"e".repeat(64),"id":id(21)})).unwrap(),
        serde_json::from_value(json!({"action":"api","protocol":"","path":"","token":"d".repeat(64)})).unwrap(),
        auth(&grant,Some(1))];
    for role in &roles { assert!(p.authorize_media(role,1000,now)); }
    assert!(p.catalog(&"a".repeat(64)).is_ok());
    assert!(p.activity_scope(&"a".repeat(64),"cam_one",false).is_ok());
    // A healthy API cannot keep the credential cohort alive past its deadline.
    // The fixture store is held by p; use a separate private store for maintenance.
    let maintenance_directory=f.directory.join("maintenance");fs::create_dir(&maintenance_directory).unwrap();
    fs::set_permissions(&maintenance_directory,fs::Permissions::from_mode(0o700)).unwrap();
    let maintenance_store=maintenance_directory.join("revocations.json");fs::write(&maintenance_store,b"{\"version\":1,\"revoked\":[]}").unwrap();
    fs::set_permissions(&maintenance_store,fs::Permissions::from_mode(0o600)).unwrap();
    let mut settings=f.settings.clone();settings.revocations_file=maintenance_store;
    let (_,_,maintenance)=nxrs_gateway_control::managed_routers(f.config.clone(),Some(settings)).unwrap();
    let failure=tokio::time::timeout(Duration::from_secs(4),maintenance.run()).await.expect("credential expiry must retire healthy management");
    assert_eq!(failure,nxrs_gateway_control::SupervisionFailure::CredentialsUnavailable);
    for role in &roles { assert!(!p.authorize_media(role,1000,Instant::now())); }
    assert_eq!(p.catalog(&"a".repeat(64)).unwrap_err(),StatusCode::UNAUTHORIZED);
    assert_eq!(p.activity_scope(&"a".repeat(64),"cam_one",false).unwrap_err(),StatusCode::UNAUTHORIZED);
    assert_eq!(p.issue(&"a".repeat(64),"cam_one",1000,Instant::now()).unwrap_err(),StatusCode::UNAUTHORIZED);
}


#[tokio::test]
async fn credential_deadline_cancels_a_stalled_management_exchange() {
    use std::sync::{atomic::AtomicU8,Arc};
    let (mut f,_server)=management_fixture(Arc::new(AtomicU8::new(4))).await;
    let wall=std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs();
    f.config.credential_window=Some(nxrs_gateway_control::credentials::Window { not_before:wall-1,expires_at:wall+2 });
    let (_,_,maintenance)=nxrs_gateway_control::managed_routers(f.config.clone(),Some(f.settings.clone())).unwrap();
    let failure=tokio::time::timeout(Duration::from_secs(3),maintenance.run()).await
        .expect("credential deadline must bound even a stalled management exchange");
    assert_eq!(failure,nxrs_gateway_control::SupervisionFailure::CredentialsUnavailable);
}

fn backend_auth(role: nxrs_gateway_control::security::BackendRole, user: &str, secret: &str, n: u64) -> Auth {
    let action=match role { nxrs_gateway_control::security::BackendRole::Publisher=>"publish",
        nxrs_gateway_control::security::BackendRole::Recorder=>"read" };
    serde_json::from_value(json!({"action":action,"protocol":"rtsp","path":"one","user":user,
        "password":secret,"token":secret,"id":id(n)})).unwrap()
}
fn backend_remote(n: u64, state: &str, user: &str) -> nxrs_gateway_control::security::RemoteRtspSession {
    nxrs_gateway_control::security::RemoteRtspSession { id:id(n),state:state.into(),path:"one".into(),user:user.into() }
}
fn add_recorder(f: &mut Fixture) {
    f.config.activity=Some(nxrs_camera_activity::store::Settings { protection:None,directory:f.directory.join("activity"),
        camera_ids:vec!["cam_one".into()],retention_seconds:86400,max_bytes:64*1024*1024,max_events:100 });
    f.config.recorders.push(nxrs_gateway_control::config::Recorder { username:"recorder".into(),
        camera_id:"cam_one".into(),token_sha256:digest(&"e".repeat(64)) });
}

#[test]
fn backend_identity_denial_is_role_scoped_and_survives_rotated_credentials_and_restart() {
    use nxrs_gateway_control::security::BackendRole::{Publisher, Recorder};
    let mut f=Fixture::new();add_recorder(&mut f);let now=Instant::now();let mut p=f.policy();
    let publisher=backend_auth(Publisher,"publisher",&"c".repeat(64),1);
    let recorder=backend_auth(Recorder,"recorder",&"e".repeat(64),2);
    assert!(!p.authorize_media(&publisher,1000,now));
    p.synchronized(true,now);
    assert!(p.authorize_media(&publisher,1000,now));assert!(p.authorize_media(&recorder,1000,now));
    assert_eq!(p.revoke_backend(Recorder,"publisher"),Err(StatusCode::NOT_FOUND));
    assert!(p.authorize_media(&publisher,1000,now));
    assert_eq!(p.revoke_backend(Publisher,"publisher"),Ok(StatusCode::ACCEPTED));
    assert!(!p.authorize_media(&publisher,1000,now));assert!(p.authorize_media(&recorder,1000,now));
    let remote=[backend_remote(1,"publish","publisher"),backend_remote(2,"read","recorder")];
    assert_eq!(p.plan_backend(&remote,now),vec![id(1)]);
    assert!(p.backend_still_forbidden(&id(1)));assert!(!p.backend_still_forbidden(&id(2)));
    assert_eq!(p.backend_revocation_status(Publisher,"publisher",now).unwrap()["revoked"],true);
    assert_eq!(serde_json::from_slice::<serde_json::Value>(&fs::read(&f.settings.revocations_file).unwrap()).unwrap()["version"],2);
    drop(p);
    f.config.publishers[0].token_sha256=digest(&"f".repeat(64));
    let mut p=f.policy();p.synchronized(true,now);
    assert!(!p.authorize_media(&backend_auth(Publisher,"publisher",&"f".repeat(64),3),1000,now));
    assert!(p.authorize_media(&recorder,1000,now));
    // Moving a denied globally unique enrollment ID into another role does
    // not make it a new authorized identity.
    drop(p);f.config.publishers.clear();
    f.config.viewers.push(nxrs_gateway_control::config::Viewer { viewer_id:"publisher".into(),
        site_id:"site_one".into(),camera_ids:vec!["cam_one".into()],token_sha256:digest(&"f".repeat(64)),manage_recordings:false });
    assert_eq!(f.policy().catalog(&"f".repeat(64)),Err(StatusCode::FORBIDDEN));
}
#[test]
fn failed_backend_persistence_denies_all_new_rtsp_and_selects_active_sessions_until_full_retry() {
    use nxrs_gateway_control::security::BackendRole::{Publisher, Recorder};
    let mut f=Fixture::new();add_recorder(&mut f);let now=Instant::now();let mut p=f.policy();p.synchronized(true,now);
    let publisher=backend_auth(Publisher,"publisher",&"c".repeat(64),1);
    let recorder=backend_auth(Recorder,"recorder",&"e".repeat(64),2);
    assert!(p.authorize_media(&publisher,1000,now));assert!(p.authorize_media(&recorder,1000,now));
    let saved=f.directory.join("temporarily-unavailable");fs::rename(&f.settings.revocations_file,&saved).unwrap();
    assert_eq!(p.revoke_backend(Recorder,"recorder"),Err(StatusCode::SERVICE_UNAVAILABLE));
    assert!(!p.authorize_media(&publisher,1000,now));assert!(!p.authorize_media(&recorder,1000,now));
    let remote=[backend_remote(1,"publish","publisher"),backend_remote(2,"read","recorder")];
    let mut selected=p.plan_backend(&remote,now);selected.sort();
    assert_eq!(selected,vec![id(1),id(2)]);
    fs::rename(saved,&f.settings.revocations_file).unwrap();
    assert_eq!(p.revoke_backend(Publisher,"publisher"),Ok(StatusCode::ACCEPTED));
    drop(p);let mut p=f.policy();p.synchronized(true,now);
    assert!(!p.authorize_media(&publisher,1000,now));assert!(!p.authorize_media(&recorder,1000,now));
}
#[test]
fn backend_denial_requires_durable_enforcement_and_never_uses_viewer_authority() {
    use nxrs_gateway_control::security::BackendRole::{Publisher, Recorder};
    let f=Fixture::new();let mut p=GatewayPolicy::new(f.config.clone(),None).unwrap();
    assert_eq!(p.revoke_backend(Publisher,"publisher"),Err(StatusCode::SERVICE_UNAVAILABLE));
    let mut p=f.policy();
    assert_eq!(p.revoke_backend(Publisher,"viewer_a"),Err(StatusCode::NOT_FOUND));
    assert_eq!(p.revoke_backend(Recorder,"viewer_a"),Err(StatusCode::NOT_FOUND));
    assert!(p.catalog(&"a".repeat(64)).is_ok());
}
