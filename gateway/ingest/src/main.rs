#![forbid(unsafe_code)]
use nxrs_camera_ingest::{config::Config, runner, Error, Result};
use std::path::PathBuf;
use tokio::sync::watch;

#[cfg(unix)]
async fn execute() -> Result<()> {
    let mut args=std::env::args_os().skip(1);
    let path=PathBuf::from(args.next().ok_or(Error::Configuration)?);
    let mut require_tls = false;
    let mut check = false;
    let mut require_isolation = false;
    for flag in args {
        if flag == "--require-camera-tls" && !require_tls { require_tls = true; }
        else if flag == "--require-media-isolation" && !require_isolation { require_isolation = true; }
        else if flag == "--check-config" && !check { check = true; }
        else { return Err(Error::Configuration); }
    }
    let config=Config::load(&path)?;
    config.check_isolation(&path, require_isolation)?;
    if let Some(launcher) = &config.media_sandbox { runner::check_encoder_isolation(launcher).await?; }
    if require_tls && config.camera_tls.is_none() { return Err(Error::Configuration); }
    if check { return nxrs_camera_ingest::check(&config, require_tls); }
    let mut term=tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let (stop,rx)=watch::channel(false);
    let signal=tokio::spawn(async move {
        tokio::select! { _=tokio::signal::ctrl_c()=>{}, _=term.recv()=>{} }
        let _=stop.send(true);
    });
    let result=runner::run(config,rx).await;
    signal.abort(); let _=signal.await;
    result
}
#[cfg(not(unix))]
async fn execute() -> Result<()> { Err(Error::Configuration) }
#[tokio::main(flavor="current_thread")]
async fn main() {
    if let Err(category)=execute().await {
        eprintln!("nxrs-camera-ingest: {category}");
        std::process::exit(1);
    }
}
