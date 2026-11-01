//! Operator-invoked upload using the camera's existing exact enrollment policy.
use crate::{config::Config, tls::Prepared, Error, Result};
use firmware_update as wire;
use std::{
    fs::File,
    io::{Read, Seek, SeekFrom},
    path::Path,
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
};

/// Returns after RAM delivery only. MCUboot authentication, trial health and
/// confirmation occur after the device has joined its workers and rebooted.
pub async fn upload(config: &Config, path: &Path) -> Result<()> {
    config.validate()?;
    if config.camera_tls.is_none() {
        return Err(Error::Authentication);
    }
    // Reject ordinary FIFO/device paths before opening. The operator supplies
    // a regular local file; synchronous filesystem I/O is not timer-preempted.
    if !std::fs::metadata(path)?.is_file() {
        return Err(Error::Configuration);
    }
    let mut file = File::open(path)?;
    let metadata = file.metadata()?;
    let length = u32::try_from(metadata.len()).map_err(|_| Error::Configuration)?;
    if !metadata.is_file() || length < 32 {
        return Err(Error::Configuration);
    }
    let mut magic = [0; 4];
    file.read_exact(&mut magic)?;
    if u32::from_le_bytes(magic) != 0x96f3_b83d {
        return Err(Error::Configuration);
    }
    file.seek(SeekFrom::Start(0))?;
    let prepared = Prepared::load(config)?;
    let transfer = async {
        let socket = TcpStream::connect(config.camera).await?;
        let mut io = prepared.connect(socket).await?;
        io.write_all(wire::REQUEST).await?;
        io.flush().await?;
        let mut ready = [0; 8];
        io.read_exact(&mut ready).await?;
        if &ready[..4] != wire::READY {
            return Err(Error::Protocol);
        }
        wire::length(
            length.to_be_bytes(),
            u32::from_be_bytes(ready[4..].try_into().unwrap()),
        )
        .map_err(|_| Error::Configuration)?;
        io.write_all(&length.to_be_bytes()).await?;
        let mut remaining = length;
        let mut bytes = [0; 8192];
        while remaining > 0 {
            let count = file.read(&mut bytes[..(remaining as usize).min(8192)])?;
            if count == 0 {
                return Err(Error::Protocol);
            }
            io.write_all(&bytes[..count]).await?;
            remaining -= count as u32;
        }
        if file.read(&mut bytes[..1])? != 0 {
            return Err(Error::Protocol);
        }
        io.write_all(wire::END).await?;
        io.flush().await?;
        let mut received = [0; 4];
        io.read_exact(&mut received).await?;
        if &received != wire::RECEIVED {
            return Err(Error::Protocol);
        }
        io.write_all(wire::COMMIT).await?;
        io.flush().await?;
        Ok(())
    };
    tokio::select! {
        biased;
        error = prepared.guard() => Err(error),
        result = tokio::time::timeout(Duration::from_secs(120), transfer) => result.map_err(|_| Error::Timeout)?,
    }
}
