use nxrs_camera_activity::{
    now_ms,
    store::{Error, Settings, Store, MAX_CLIP_BYTES},
};
use std::os::unix::fs::{symlink, DirBuilderExt, PermissionsExt};
use std::{fs, path::PathBuf};

struct Fixture {
    path: PathBuf,
    store: Store,
}
impl Fixture {
    fn new(max_events: usize) -> Self {
        let mut id = [0; 16];
        getrandom::fill(&mut id).unwrap();
        let name: String = id.iter().map(|b| format!("{b:02x}")).collect();
        let path = std::env::temp_dir()
            .canonicalize()
            .unwrap()
            .join(format!("nxrs-activity-{name}"));
        fs::DirBuilder::new().mode(0o700).create(&path).unwrap();
        let store = Store::open(Settings {
            protection: None,
            directory: path.clone(),
            camera_ids: vec!["one".into(), "two".into()],
            retention_seconds: 60,
            max_bytes: MAX_CLIP_BYTES,
            max_events,
        })
        .unwrap();
        Self { path, store }
    }
    fn event(&self, camera: &str, time: u64) -> nxrs_camera_activity::store::Event {
        self.store
            .publish(
                camera,
                time,
                time + 100,
                b"test-only-media",
                b"test-only-thumbnail",
                false,
            )
            .unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

#[test]
fn legacy_health_loads_and_recovery_timings_survive_store_reopen() {
    let f = Fixture::new(20);
    let mut health: nxrs_camera_activity::store::Health =
        serde_json::from_value(serde_json::json!({
            "updated_at_ms": 1000, "last_frame_at_ms": 900, "state": "monitoring",
            "reconnects": 1, "frames_observed": 10, "clips_created": 0, "clips_dropped": 0
        }))
        .unwrap();
    assert!(health.progress.is_none());
    health.progress = Some(nxrs_camera_activity::progress::ProgressHealth {
        observer_run: 2,
        recoveries: 1,
        last_recovery_ms: Some(5000),
        max_recovery_ms: 5000,
        max_frame_gap_ms: 5000,
        frame_gaps_over_30s: 0,
    });
    f.store.write_health("one", &health).unwrap();
    let reopened = Store::open(f.store.settings().clone()).unwrap();
    let progress = reopened.health("one").unwrap().unwrap().progress.unwrap();
    assert_eq!(progress.observer_run, 2);
    assert_eq!(progress.last_recovery_ms, Some(5000));
}

#[test]
fn publication_reopens_and_media_is_camera_scoped() {
    let f = Fixture::new(20);
    let a = f.event("one", now_ms() - 1000);
    let reopened = Store::open(f.store.settings().clone()).unwrap();
    assert_eq!(
        reopened.page("one", None).unwrap().events.as_slice(),
        std::slice::from_ref(&a)
    );
    assert!(reopened.page("two", None).unwrap().events.is_empty());
    assert_eq!(
        reopened.media("two", &a.event_id, false),
        Err(Error::NotFound)
    );
    assert_eq!(reopened.media("one", "../one", false), Err(Error::NotFound));
    assert_eq!(
        reopened.media("one", &a.event_id, false).unwrap(),
        b"test-only-media"
    );
    for name in ["event.json", "clip.mp4", "thumbnail.jpg"] {
        assert_eq!(
            fs::metadata(f.path.join("events").join(&a.event_id).join(name))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
    }
}
#[test]
fn retained_footage_survives_expiry_and_quota_pressure() {
    let f = Fixture::new(1);
    let a = f.event("one", now_ms() - 1000);
    f.store.keep("one", &a.event_id, true).unwrap();
    f.store.prune(now_ms() + 120_000).unwrap();
    assert!(f.store.page("one", None).unwrap().events[0].kept);
    assert_eq!(
        f.store
            .publish("two", now_ms(), now_ms() + 100, b"x", b"x", false),
        Err(Error::Full)
    );
    assert!(f.store.media("one", &a.event_id, false).is_ok());
    f.store.keep("one", &a.event_id, false).unwrap();
    let b = f.event("two", now_ms());
    assert!(f.store.page("one", None).unwrap().events.is_empty());
    assert_eq!(
        f.store.page("two", None).unwrap().events[0].event_id,
        b.event_id
    );
}
#[test]
fn corruption_is_visible_and_does_not_erase_saved_media() {
    let f = Fixture::new(10);
    let a = f.event("one", now_ms() - 1000);
    fs::write(
        f.path.join("events").join(&a.event_id).join("event.json"),
        b"broken",
    )
    .unwrap();
    assert!(matches!(f.store.page("one", None), Err(Error::Corrupt)));
    assert!(matches!(
        Store::open(f.store.settings().clone()),
        Err(Error::Corrupt)
    ));
    assert!(f
        .path
        .join("events")
        .join(a.event_id)
        .join("clip.mp4")
        .is_file());
}
#[test]
fn symlink_media_and_aliased_roots_are_rejected() {
    let f = Fixture::new(10);
    let a = f.event("one", now_ms() - 1000);
    let clip = f.path.join("events").join(&a.event_id).join("clip.mp4");
    fs::remove_file(&clip).unwrap();
    symlink(f.path.join("store.lock"), &clip).unwrap();
    assert!(matches!(
        f.store.media("one", &a.event_id, false),
        Err(Error::Corrupt)
    ));
    let alias = f.path.join("alias");
    symlink(&f.path, &alias).unwrap();
    let mut settings = f.store.settings().clone();
    settings.directory = alias;
    assert!(Store::open(settings).is_err());
}
#[test]
fn paging_has_no_duplicates_and_expiry_is_explicit() {
    let f = Fixture::new(100);
    let now = now_ms() - 1000;
    for i in 0..55 {
        f.event("one", now + i);
    }
    let first = f.store.page("one", None).unwrap();
    assert_eq!(first.events.len(), 50);
    let second = f.store.page("one", first.next_cursor.as_deref()).unwrap();
    assert_eq!(second.events.len(), 5);
    assert!(second.next_cursor.is_none());
    assert!(second
        .events
        .iter()
        .all(|e| !first.events.iter().any(|a| a.event_id == e.event_id)));
    f.store.prune(now + 70_000).unwrap();
    assert!(f.store.page("one", None).unwrap().events.is_empty());
}
#[test]
fn interprocess_lock_prevents_lost_updates() {
    let f = Fixture::new(10);
    let file = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(f.path.join("store.lock"))
        .unwrap();
    file.lock().unwrap();
    assert!(matches!(f.store.page("one", None), Err(Error::Busy)));
    drop(file);
    assert!(f.store.page("one", None).is_ok());
}

#[test]
fn kept_history_is_filtered_before_pagination() {
    let f = Fixture::new(100);
    let now = now_ms();
    let kept = f.event("one", now - 10_000);
    f.store.keep("one", &kept.event_id, true).unwrap();
    for i in 0..55 {
        f.event("one", now - 1000 + i);
    }
    assert!(!f
        .store
        .page("one", None)
        .unwrap()
        .events
        .iter()
        .any(|e| e.kept));
    let page = f.store.kept_page("one", None).unwrap();
    assert_eq!(page.events.len(), 1);
    assert_eq!(page.events[0].event_id, kept.event_id);
    assert!(f.store.kept_page("two", None).unwrap().events.is_empty());
}

#[test]
fn protected_store_never_creates_a_plaintext_fallback_directory() {
    let f = Fixture::new(20);
    let mount = f.path.join("plain-mount");
    fs::create_dir(&mount).unwrap();
    let mut settings = f.store.settings().clone();
    settings.directory = mount.join("activity");
    settings.protection = Some(nxrs_camera_activity::protection::Volume {
        layout_version: 2,
        mount: mount.clone(),
        luks_uuid: "01234567-89ab-cdef-0123-456789abcdef".into(),
    });
    assert_eq!(Store::open(settings.clone()).err(), Some(Error::Protection));
    assert!(!settings.directory.exists());
    fs::remove_dir(mount).unwrap();
    assert_eq!(Store::open(settings.clone()).err(), Some(Error::Protection));
    assert!(!settings.directory.exists());
}

#[test]
fn protected_store_contract_rejects_outside_roots_and_invalid_identities() {
    let f = Fixture::new(20);
    let volume = nxrs_camera_activity::protection::Volume {
        layout_version: 2,
        mount: f.path.clone(),
        luks_uuid: "01234567-89ab-cdef-0123-456789abcdef".into(),
    };
    assert_eq!(volume.validate(&f.path.join("activity")), Ok(()));
    let old =
        serde_json::json!({"mount":f.path,"luks_uuid":"01234567-89ab-cdef-0123-456789abcdef"});
    assert!(serde_json::from_value::<nxrs_camera_activity::protection::Volume>(old).is_err());
    let mut invalid = volume.clone();
    invalid.layout_version = 1;
    assert_eq!(
        invalid.validate(&f.path.join("activity")),
        Err(Error::Invalid)
    );

    for directory in [
        f.path.clone(),
        f.path.join("other"),
        f.path.join("activity").join("elsewhere"),
    ] {
        assert_eq!(volume.validate(&directory), Err(Error::Invalid));
    }
    for uuid in [
        "../../plain",
        "01234567-89ab-cdef-0123-456789abcdeg",
        "0123456789abcdef0123456789abcdef",
    ] {
        let mut bad = volume.clone();
        bad.luks_uuid = uuid.into();
        assert_eq!(bad.validate(&f.path.join("activity")), Err(Error::Invalid));
    }
}
