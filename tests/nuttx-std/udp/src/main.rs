//! Direct std socket qualification; no project HAL, libc dependency, or FFI.
use std::io::{self, ErrorKind};
use std::net::{Ipv4Addr, SocketAddr, UdpSocket};
use std::sync::mpsc::sync_channel;
use std::thread;
use std::time::{Duration, Instant};

const COUNT: u8 = 16;
const WAIT: Duration = Duration::from_secs(2);

fn loopback() -> io::Result<UdpSocket> {
    UdpSocket::bind((Ipv4Addr::LOCALHOST, 0))
}

fn receive(socket: &UdpSocket, expected: &[u8], peer: SocketAddr) -> io::Result<()> {
    let mut bytes = [0u8; 64];
    let (len, source) = socket.recv_from(&mut bytes)?;
    assert_eq!(source, peer);
    assert_eq!(&bytes[..len], expected);
    Ok(())
}


#[cfg(target_os = "nuttx")]
fn qualify_packet_sink(receiver: &UdpSocket, endpoint: SocketAddr) -> io::Result<()> {
    use transport::Transport;

    let mut sender = transport::UdpSender::connect(
        [127, 0, 0, 1],
        endpoint.port(),
    )
    .map_err(|error| io::Error::other(format!("NuttX PacketSink connect: {error:?}")))?;
    let bound = sender.local_addr()?;
    assert_ne!(bound.port(), 0);
    // A socket bound to 0.0.0.0 keeps that wildcard in getsockname() on the
    // pinned NuttX stack, while loopback routing emits 127.0.0.1 on the wire.
    // The ephemeral source port must be preserved across that routing choice.
    let source = SocketAddr::from((Ipv4Addr::LOCALHOST, bound.port()));
    sender
        .send(b"packet-sink")
        .map_err(|error| io::Error::other(format!("NuttX PacketSink send: {error:?}")))?;
    receive(receiver, b"packet-sink", source)?;
    println!("NXRS_UDP_PROVIDER_PASS");
    Ok(())
}

fn qualify() -> io::Result<()> {
    let receiver = loopback()?;
    let endpoint = receiver.local_addr()?;
    assert_eq!(endpoint.ip(), Ipv4Addr::LOCALHOST);
    assert_ne!(endpoint.port(), 0);
    receiver.set_read_timeout(Some(WAIT))?;
    assert_eq!(receiver.read_timeout()?, Some(WAIT));
    assert_eq!(
        receiver.set_read_timeout(Some(Duration::ZERO)).unwrap_err().kind(),
        ErrorKind::InvalidInput
    );
    receiver.set_read_timeout(None)?;
    receiver.set_nonblocking(true)?;
    assert_eq!(receiver.recv(&mut [0; 1]).unwrap_err().kind(), ErrorKind::WouldBlock);
    receiver.set_nonblocking(false)?;
    receiver.set_read_timeout(Some(Duration::from_millis(20)))?;
    let start = Instant::now();
    let error = receiver.recv(&mut [0; 1]).unwrap_err();
    assert!(matches!(error.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut));
    assert!(start.elapsed() >= Duration::from_millis(10));
    receiver.set_read_timeout(Some(WAIT))?;

    let sender = loopback()?;
    let source = sender.local_addr()?;
    assert_ne!(source, endpoint);
    sender.connect(endpoint)?;
    assert_eq!(sender.peer_addr()?, endpoint);
    sender.set_nonblocking(true)?;
    let (ack, replies) = sync_channel(1);
    let worker = thread::Builder::new().stack_size(65_536).spawn(move || -> io::Result<()> {
        for n in 0..COUNT {
            let packet = [n, 0, 127, 128, 255];
            assert_eq!(sender.send(&packet)?, packet.len());
            assert_eq!(replies.recv_timeout(WAIT).unwrap(), n);
        }
        Ok(())
    })?;
    for n in 0..COUNT {
        receive(&receiver, &[n, 0, 127, 128, 255], source)?;
        println!("NXRS_UDP_PACKET {n} 007f80ff");
        ack.send(n).unwrap();
    }
    worker.join().expect("UDP sender panicked")?;

    // try_clone shares the socket but owns another descriptor. Dropping either
    // handle must not invalidate the other one, nor an independent socket.
    let original = loopback()?;
    let original_addr = original.local_addr()?;
    let cloned = original.try_clone()?;
    assert_eq!(cloned.local_addr()?, original_addr);
    drop(original);
    assert_eq!(cloned.send_to(b"clone", endpoint)?, 5);
    receive(&receiver, b"clone", original_addr)?;
    drop(cloned);

    let independent = loopback()?;
    let independent_addr = independent.local_addr()?;
    independent.connect(endpoint)?;
    assert_eq!(independent.send(b"owned")?, 5);
    receive(&receiver, b"owned", independent_addr)?;
    assert_eq!(independent.send(&[])?, 0);
    receive(&receiver, &[], independent_addr)?;
    assert!(independent.take_error()?.is_none());

    #[cfg(target_os = "nuttx")]
    qualify_packet_sink(&receiver, endpoint)?;
    drop(independent);

    // No SO_REUSEADDR: successful bind proves the receiver released its port.
    drop(receiver);
    let rebound = UdpSocket::bind(endpoint)?;
    assert_eq!(rebound.local_addr()?, endpoint);
    drop(rebound);
    println!("NXRS_UDP_REPORT {{\"messages\":16,\"joined_workers\":1,\"nonblocking\":true,\"timeout\":true,\"cloned_owner\":true,\"independent_owner\":true,\"empty_datagram\":true,\"rebind\":true}}");
    Ok(())
}

fn main() -> std::process::ExitCode {
    println!("NXRS_UDP_ENTERED");
    match std::env::args().nth(1).as_deref() {
        Some("fail") => {
            println!("NXRS_UDP_INJECTED_FAILURE");
            std::process::exit(7);
        }
        Some("pass") => {}
        _ => return std::process::ExitCode::FAILURE,
    }
    match qualify() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("NXRS_UDP_ERROR: {error}");
            std::process::ExitCode::FAILURE
        }
    }
}
