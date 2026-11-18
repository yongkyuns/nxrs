//! Versioned deployment inputs for one existing camera on the gateway.
use crate::{
    credential,
    media::{Reader, Video},
    occupancy::Feed,
    persistence::Store,
    platform,
};
use serde::Deserialize;
use std::{
    io::Read,
    net::{Ipv4Addr, SocketAddr, UdpSocket},
    path::{Path, PathBuf},
};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Deployment {
    version: u8,
    site_id: String,
    camera_id: String,
    control: SocketAddr,
    rtsp: SocketAddr,
    media_token_file: PathBuf,
    detection_token_file: PathBuf,
    pub factory_data: PathBuf,
    pub state_directory: PathBuf,
    pub ice_address: Ipv4Addr,
    pub snapshot_ffmpeg: Option<PathBuf>,
}

impl Deployment {
    pub fn load(path: &Path) -> Result<Self, &'static str> {
        if !path.is_absolute() {
            return Err("expected absolute deployment path");
        }
        let mut bytes = Vec::new();
        platform::open_file(path, false, false)
            .and_then(|file| file.take(8193).read_to_end(&mut bytes))
            .map_err(|_| "unreadable Matter deployment")?;
        if bytes.len() > 8192 {
            return Err("Matter deployment too large");
        }
        let config: Self =
            serde_json::from_slice(&bytes).map_err(|_| "invalid Matter deployment")?;
        if config.version != 1
            || !credential::identifier(&config.site_id)
            || !credential::identifier(&config.camera_id)
            || [config.control, config.rtsp]
                .iter()
                .any(|s| !s.ip().is_loopback() || s.port() == 0)
            || config.control == config.rtsp
            || config
                .snapshot_ffmpeg
                .as_ref()
                .is_some_and(|path| !path.is_absolute() || !path.is_file())
            || config.ice_address.is_unspecified()
            || config.ice_address.is_multicast()
            || config.ice_address.is_broadcast()
            || [
                &config.factory_data,
                &config.state_directory,
                &config.media_token_file,
                &config.detection_token_file,
            ]
            .iter()
            .any(|p| !p.is_absolute())
        {
            return Err("invalid Matter deployment");
        }
        Ok(config)
    }

    pub async fn inputs(&self, initializing: bool) -> Result<(Reader, Video, Feed), &'static str> {
        UdpSocket::bind((self.ice_address, 0)).map_err(|_| "ICE address unavailable")?;
        let (reader, video) = Reader::catalogue(
            self.control,
            self.rtsp,
            &self.site_id,
            &self.camera_id,
            &self.media_token_file,
        )?;
        if initializing {
            reader.check_mapping().await?;
        }
        let feed = Feed::open(self.control, &self.camera_id, &self.detection_token_file)?
            .at_site(&self.site_id);
        Ok((reader, video, feed))
    }

    /// Endpoints 1 and 3 belong to this camera throughout the enrollment.
    /// A new media path or credential can serve the same camera after restart.
    pub fn bind(
        &self,
        store: &mut Store,
        key: u16,
        initializing: bool,
        new_id: impl FnOnce() -> [u8; 16],
    ) -> Result<(String, String), &'static str> {
        use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
        let identity = format!("v1:{}.{}", self.site_id, self.camera_id);
        let mut buffer = [0; 148];
        let id: [u8; 16] = match store
            .load(key, &mut buffer)
            .map_err(|_| "unreadable camera identity")?
        {
            Some(record) => record
                .strip_prefix(identity.as_bytes())
                .and_then(|id| id.try_into().ok())
                .ok_or("camera identity differs from enrollment")?,
            None if initializing => {
                let id = new_id();
                let mut record = identity.into_bytes();
                record.extend_from_slice(&id);
                store
                    .store(key, &record)
                    .map_err(|_| "cannot bind camera identity")?;
                id
            }
            _ => return Err("missing camera identity"),
        };
        let prefix = URL_SAFE_NO_PAD.encode(id);
        Ok((format!("{prefix}-cam"), format!("{prefix}-occ")))
    }
}
