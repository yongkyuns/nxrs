use std::{fs, num::NonZeroU64};

#[test]
fn service_diagnostics_reach_the_desktop_rotating_archive() {
    let dir = std::env::temp_dir().join(format!("nxrs-file-logging-{}", std::process::id()));
    fs::create_dir(&dir).unwrap();
    let active = dir.join("system.log");
    let logger = logging::start_file(
        active.clone(),
        NonZeroU64::new(256).unwrap(),
        log::LevelFilter::Info,
    )
    .unwrap();
    for _ in 0..18 {
        nxrs_log_qualification::configure(false);
    }
    nxrs_log_qualification::configure(true);
    assert_eq!(logger.finish().unwrap(), 0);

    let previous = fs::read(dir.join("system.log.1")).unwrap();
    let active = fs::read(active).unwrap();
    assert_eq!(previous.len(), 256);
    assert!(active.len() <= 256);
    assert!(std::str::from_utf8(&previous)
        .unwrap()
        .contains("[INFO navigation::observations] configured 1 IMU channels, GNSS disabled\n"));
    assert!(std::str::from_utf8(&active)
        .unwrap()
        .ends_with("[INFO navigation::observations] configured 1 IMU channels, GNSS enabled\n"));
    assert_eq!(fs::read_dir(&dir).unwrap().count(), 2);
    println!(
        "Observation service records verified in system.log ({} bytes) and system.log.1 ({} bytes)",
        active.len(),
        previous.len()
    );
    fs::remove_dir_all(dir).unwrap();
}
