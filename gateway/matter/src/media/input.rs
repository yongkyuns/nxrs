//! Resolve the selected catalogue camera on each RTSP connection.
use super::*;
use http_body_util::{BodyExt, Empty, Limited};
use hyper::{client::conn::http1, header, Request, StatusCode};
use hyper_util::rt::TokioIo;
use serde::Deserialize;
use tokio::net::TcpStream;

pub(super) enum Input {
    Fixed(Resolved),
    Catalogue {
        control: SocketAddr,
        media: SocketAddr,
        site: String,
        camera: String,
        token: String,
    },
}

#[derive(Clone)]
pub(super) struct Resolved {
    pub(super) url: Url,
    pub(super) credentials: Credentials,
}

#[derive(Deserialize)]
struct Mapping {
    version: u8,
    camera_id: String,
    site_id: String,
    stream_path: String,
    username: String,
}

impl Input {
    pub(super) async fn resolve(&self) -> Result<Resolved, ()> {
        let (control, media, site, camera, token) = match self {
            Self::Fixed(input) => return Ok(input.clone()),
            Self::Catalogue {
                control,
                media,
                site,
                camera,
                token,
            } => (control, media, site, camera, token),
        };
        let request = async {
            let socket = TcpStream::connect(control).await.map_err(|_| ())?;
            let (mut client, connection) = http1::Builder::new()
                .max_buf_size(16 * 1024)
                .handshake(TokioIo::new(socket))
                .await
                .map_err(|_| ())?;
            let mut auth =
                header::HeaderValue::from_str(&format!("Bearer {token}")).map_err(|_| ())?;
            auth.set_sensitive(true);
            let req = Request::get(format!("/service/v1/sites/{site}/cameras/{camera}/source"))
                .header(header::HOST, control.to_string())
                .header(header::AUTHORIZATION, auth)
                .header(header::CONNECTION, "close")
                .body(Empty::<Bytes>::new())
                .map_err(|_| ())?;
            let read = async move {
                let resp = client.send_request(req).await.map_err(|_| ())?;
                if resp.status() != StatusCode::OK {
                    return Err(());
                }
                let body = Limited::new(resp.into_body(), 1024)
                    .collect()
                    .await
                    .map_err(|_| ())?
                    .to_bytes();
                serde_json::from_slice::<Mapping>(&body).map_err(|_| ())
            };
            let (_, result) = tokio::join!(connection, read);
            result
        };
        let entry = time::timeout(Duration::from_secs(2), request)
            .await
            .map_err(|_| ())??;
        if entry.version != 1
            || entry.camera_id != *camera
            || entry.site_id != *site
            || !crate::credential::identifier(&entry.stream_path)
            || !crate::credential::identifier(&entry.username)
        {
            return Err(());
        }
        let url = Url::parse(&format!(
            "rtsp://{media}/{}?nxrs_source={site}.{camera}",
            entry.stream_path
        ))
        .map_err(|_| ())?;
        Ok(Resolved {
            url,
            credentials: Credentials {
                username: entry.username,
                password: token.clone(),
            },
        })
    }
}

impl Resolved {
    pub(super) async fn describe(&self) -> Result<Session<retina::client::Described>, ()> {
        let options = SessionOptions::default()
            .creds(Some(self.credentials.clone()))
            .teardown(TeardownPolicy::Never);
        Session::describe(self.url.clone(), options)
            .await
            .map_err(|_| ())
    }
}
