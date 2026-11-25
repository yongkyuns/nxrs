//! Bounded requests to the local control listener under the publisher identity.
use crate::{Error, Result};
use std::{net::SocketAddrV4, time::Duration};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
    time::timeout,
};

pub(crate) async fn publisher_post(
    control: SocketAddrV4,
    camera: &str,
    user: &str,
    token: &str,
    path: &str,
    body: &[u8],
) -> Result<Vec<u8>> {
    timeout(Duration::from_millis(500), async {
        let mut socket=TcpStream::connect(control).await?;
        let request=format!("POST /internal/cameras/{camera}/{path} HTTP/1.1\r\nHost: {control}\r\nAuthorization: Bearer {token}\r\nX-Nxrs-Publisher: {user}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",body.len());
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
