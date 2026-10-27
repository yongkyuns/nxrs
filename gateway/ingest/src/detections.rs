//! Latest-value forwarding has its own task and deadlines; video never awaits HTTP.
use crate::{config::DetectionConfig, protocol::Frame, Error, Result};
use detection_wire::{Source, Update};
use std::time::Duration;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
    sync::watch,
    time::{timeout, Instant},
};

#[derive(Clone)]
pub(crate) struct Sample {
    pub frame: Frame,
    pub source: Source,
    pub received: Instant,
}

async fn post(
    c: &DetectionConfig,
    user: &str,
    token: &str,
    path: &str,
    body: &[u8],
) -> Result<Vec<u8>> {
    timeout(Duration::from_millis(500), async {
        let mut socket=TcpStream::connect(c.control).await?;
        let request=format!("POST /internal/cameras/{}/{path} HTTP/1.1\r\nHost: {}\r\nAuthorization: Bearer {token}\r\nX-Nxrs-Publisher: {user}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", c.camera_id,c.control,body.len());
        socket.write_all(request.as_bytes()).await?; socket.write_all(body).await?;
        // Bounded local response; no redirects, arbitrary origins, or token diagnostics.
        let mut response=Vec::new(); socket.take(8193).read_to_end(&mut response).await?;
        if response.len()>8192 { return Err(Error::Protocol); }
        let split=response.windows(4).position(|v|v==b"\r\n\r\n").ok_or(Error::Protocol)?;
        let status=response.get(..12).ok_or(Error::Protocol)?;
        if status!=b"HTTP/1.1 200" && status!=b"HTTP/1.1 204" { return Err(Error::Protocol); }
        Ok(response[split+4..].to_vec())
    }).await.map_err(|_|Error::Timeout)?
}
pub(crate) async fn run(
    c: DetectionConfig,
    user: String,
    token: String,
    mut rx: watch::Receiver<Option<Sample>>,
    mut stop: watch::Receiver<bool>,
) {
    let mut session = None;
    loop {
        if *stop.borrow() {
            return;
        }
        let work = async {
            if session.is_none() {
                let response = post(&c, &user, &token, "detection-sessions", b"{}").await?;
                let value: serde_json::Value =
                    serde_json::from_slice(&response).map_err(|_| Error::Protocol)?;
                let id = value["source_session_id"]
                    .as_str()
                    .filter(|s| {
                        s.len() == 32
                            && s.bytes()
                                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
                    })
                    .ok_or(Error::Protocol)?;
                session = Some(id.to_owned());
            }
            rx.changed().await.map_err(|_| Error::Stopped)?;
            let sample = rx.borrow_and_update().clone();
            if let Some(sample) =
                sample.filter(|s| s.received.elapsed() < Duration::from_millis(1000))
            {
                let update = Update {
                    source_session_id: session.clone().unwrap(),
                    frame_sequence: sample.frame.sequence,
                    frame_capture_ms: sample.frame.capture_ms,
                    source: sample.source,
                };
                let bytes = serde_json::to_vec(&update).map_err(|_| Error::Protocol)?;
                post(&c, &user, &token, "detections", &bytes).await?;
            }
            Ok::<(), Error>(())
        };
        let result = tokio::select! { biased; _=stop.changed()=>return, r=work=>r };
        if matches!(result, Err(Error::Stopped)) {
            return;
        }
        if result.is_err() {
            session = None;
        }
        // At most four requests/s, including failures. Latest watch value replaces backlog.
        let delay = if result.is_err() { 1000 } else { 250 };
        tokio::select! { biased; _=stop.changed()=>return, _=tokio::time::sleep(Duration::from_millis(delay))=>{} }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::{io::AsyncBufReadExt, net::TcpListener};

    async fn request(socket: TcpStream) -> (tokio::io::BufReader<TcpStream>, String, Vec<u8>) {
        let mut socket = tokio::io::BufReader::new(socket);
        let mut headers = String::new();
        loop {
            let mut line = String::new();
            assert!(socket.read_line(&mut line).await.unwrap() > 0);
            headers.push_str(&line);
            if line == "\r\n" {
                break;
            }
            assert!(headers.len() < 8192);
        }
        let length: usize = headers
            .lines()
            .find_map(|v| v.strip_prefix("Content-Length: "))
            .unwrap()
            .parse()
            .unwrap();
        assert!(length <= 8192);
        let mut body = vec![0; length];
        socket.read_exact(&mut body).await.unwrap();
        (socket, headers, body)
    }
    #[tokio::test]
    async fn forwarding_replaces_backlog_scopes_credentials_and_joins_on_stop() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let c = DetectionConfig {
            control: match listener.local_addr().unwrap() {
                std::net::SocketAddr::V4(v) => v,
                _ => unreachable!(),
            },
            camera_id: "cam_one".into(),
        };
        let (tx, rx) = watch::channel(None);
        let fixture: detection_wire::Snapshot = serde_json::from_slice(include_bytes!(
            "../../../clients/ios/Tests/NxrsMonitorCoreTests/Fixtures/detection.json"
        ))
        .unwrap();
        for sequence in 100..=102 {
            tx.send_replace(Some(Sample {
                frame: Frame {
                    length: 4,
                    sequence,
                    capture_ms: 1100,
                },
                source: fixture.source.clone().unwrap(),
                received: Instant::now(),
            }));
        }
        let (stop, stopped) = watch::channel(false);
        let publisher = tokio::spawn(run(c, "publisher".into(), "b".repeat(64), rx, stopped));
        for stage in ["detection-sessions", "detections"] {
            let (socket, _) = timeout(Duration::from_secs(2), listener.accept())
                .await
                .unwrap()
                .unwrap();
            let (mut socket, headers, body) = request(socket).await;
            assert!(headers.starts_with(&format!(
                "POST /internal/cameras/cam_one/{stage} HTTP/1.1\r\n"
            )));
            assert!(headers.contains(&format!("Authorization: Bearer {}\r\n", "b".repeat(64))));
            assert!(headers.contains("X-Nxrs-Publisher: publisher\r\n"));
            let response = if stage == "detection-sessions" {
                format!("{{\"source_session_id\":\"{}\"}}", "a".repeat(32))
            } else {
                let update: Update = serde_json::from_slice(&body).unwrap();
                assert_eq!(update.frame_sequence, 102);
                assert_eq!(update.source_session_id, "a".repeat(32));
                String::new()
            };
            socket.write_all(format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{response}", response.len()).as_bytes()).await.unwrap();
        }
        stop.send(true).unwrap();
        timeout(Duration::from_millis(200), publisher)
            .await
            .unwrap()
            .unwrap();
    }
    #[tokio::test]
    async fn stop_cancels_an_unresponsive_control_request() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let c = DetectionConfig {
            control: match listener.local_addr().unwrap() {
                std::net::SocketAddr::V4(v) => v,
                _ => unreachable!(),
            },
            camera_id: "cam_one".into(),
        };
        let (_tx, rx) = watch::channel(None);
        let (stop, stopped) = watch::channel(false);
        let publisher = tokio::spawn(run(c, "publisher".into(), "b".repeat(64), rx, stopped));
        let (socket, _) = timeout(Duration::from_secs(2), listener.accept())
            .await
            .unwrap()
            .unwrap();
        let (mut socket, _, _) = request(socket).await;
        stop.send(true).unwrap();
        timeout(Duration::from_millis(200), publisher)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(socket.read(&mut [0; 1]).await.unwrap(), 0);
    }
}
