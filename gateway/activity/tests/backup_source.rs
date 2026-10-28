//! SDK witness only: real Store publication of separately decoded synthetic media.
use nxrs_camera_activity::{
    now_ms,
    protection::Volume,
    store::{Settings, Store},
};
use std::{fs, os::unix::fs::PermissionsExt, path::PathBuf};
#[test]
#[ignore = "only the disposable encrypted-volume fixture supplies these resources"]
fn publish_known_media_for_backup() {
    let get = |name| std::env::var(name).expect("encrypted fixture resource required");
    let mount = PathBuf::from(get("NXRS_TEST_VOLUME"));
    let settings = Settings {
        directory: mount.join("activity"),
        camera_ids: vec!["cam_one".into()],
        max_bytes: 64 * 1024 * 1024,
        max_events: 100,
        retention_seconds: 3600,
        protection: Some(Volume {
            layout_version: 2,
            mount: mount.clone(),
            luks_uuid: get("NXRS_TEST_LUKS_UUID"),
        }),
    };
    let store = Store::open(settings).unwrap();
    let clip = fs::read(get("NXRS_TEST_CLIP")).unwrap();
    let thumbnail = fs::read(get("NXRS_TEST_THUMB")).unwrap();
    let now = now_ms();
    let event = store
        .publish("cam_one", now, now + 2000, &clip, &thumbnail, false)
        .unwrap();
    store.keep("cam_one", &event.event_id, true).unwrap();
    let receipt = mount.join("maintenance/fixture-event.json");
    fs::write(&receipt, serde_json::to_vec(&event).unwrap()).unwrap();
    fs::set_permissions(receipt, fs::Permissions::from_mode(0o600)).unwrap();
}
