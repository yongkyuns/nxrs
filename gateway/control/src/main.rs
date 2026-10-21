#![forbid(unsafe_code)]
use nxrs_gateway_control::{config::Config, managed_routers, security::Settings};
use std::{fs::File, io::Read, path::Path, time::Duration};
use tokio::{net::TcpListener, sync::watch};

async fn signal() {
    #[cfg(unix)] {
        let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("signal handler");
        tokio::select! { _ = tokio::signal::ctrl_c() => {}, _ = term.recv() => {} }
    }
    #[cfg(not(unix))] { let _ = tokio::signal::ctrl_c().await; }
}
async fn run() -> Result<(), &'static str> {
    let mut args = std::env::args().skip(1);
    let usage = "usage: nxrs-gateway-control CONFIG.json [--security SECURITY.json]";
    let path = args.next().ok_or(usage)?;
    let settings = match args.next() {
        None => None,
        Some(flag) if flag == "--security" => Some(Settings::read(Path::new(&args.next().ok_or(usage)?))?),
        _ => return Err(usage),
    };
    if args.next().is_some() { return Err(usage); }
    let mut bytes = Vec::new();
    File::open(path).map_err(|_| "cannot open configuration")?.take(65_537).read_to_end(&mut bytes)
        .map_err(|_| "cannot read configuration")?;
    if bytes.len() > 65_536 { return Err("configuration too large"); }
    let config: Config = serde_json::from_slice(&bytes).map_err(|_| "invalid configuration JSON")?;
    config.validate()?;
    let (control, internal, maintenance) = managed_routers(config.clone(), settings)?;
    let a = TcpListener::bind(config.listen).await.map_err(|_| "control listener bind failed")?;
    let b = TcpListener::bind(config.auth_listen).await.map_err(|_| "auth listener bind failed")?;
    let (stop, rx) = watch::channel(false);
    let mut rx1 = rx.clone(); let mut rx2 = rx;
    let servers = async {
        tokio::try_join!(
            async { axum::serve(a, control).with_graceful_shutdown(async move { let _ = rx1.changed().await; }).await },
            async { axum::serve(b, internal).with_graceful_shutdown(async move { let _ = rx2.changed().await; }).await }
        ).map_err(|_| "gateway server stopped unexpectedly")
    };
    tokio::pin!(servers);
    eprintln!("nxrs-gateway ready (loopback listeners; media readiness is separate)");
    tokio::select! {
        result = &mut servers => { result?; },
        _ = maintenance.run() => { return Err("media supervision stopped unexpectedly"); },
        _ = signal() => {
            let _ = stop.send(true);
            tokio::time::timeout(Duration::from_secs(6), &mut servers).await.map_err(|_| "shutdown deadline exceeded")??;
        }
    }
    Ok(())
}
#[tokio::main]
async fn main() {
    if let Err(message) = run().await { eprintln!("nxrs-gateway: {message}"); std::process::exit(1); }
}
