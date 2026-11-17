use super::*;
use serde_json::{json, Value};
use std::cell::RefCell;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

fn snapshot() -> Value {
    serde_json::from_str(include_str!(
        "../../../../clients/ios/Tests/NxrsMonitorCoreTests/Fixtures/detection.json"
    ))
    .unwrap()
}

fn record(s: &Value) -> Vec<u8> {
    format!(
        "event: detection\nid: {}:{}\ndata: {s}\n\n",
        s["source_session_id"].as_str().unwrap(),
        s["source"]["event_count"]
    )
    .into_bytes()
}

#[test]
fn snapshot_identity_order_and_deadline() {
    let mut s = snapshot();
    let mut cursor = Cursor::default();
    let mut read = |s: &Value, ms| cursor.read(&record(s), "cam_one", Duration::from_millis(ms));
    assert_eq!(
        read(&s, 1200).unwrap(),
        (Presence::Person, Some(Duration::from_millis(300)))
    );
    assert_eq!(read(&s, 1500).unwrap(), (Presence::Unavailable, None));
    for (key, value) in [
        ("camera_id", json!("another")),
        ("revision", json!(4)),
        ("observation_age_ms", json!(1)),
        ("source_online", json!(false)),
        ("source_session_id", json!("bad")),
    ] {
        let mut bad = s.clone();
        bad[key] = value;
        assert!(read(&bad, 0).is_err());
    }
    s["stream_elapsed_ms"] = json!(501);
    assert!(read(&s, 400).is_err());
}

#[tokio::test]
async fn silent_partial_record_expires_and_cancellation_withdraws_recovered_level() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let feed = Feed {
        control: listener.local_addr().unwrap(),
        camera: "cam_one".into(),
        authorization: header::HeaderValue::from_static("Bearer fixture"),
    };
    let observed = RefCell::new(Vec::new());
    let mut s = snapshot();
    let server = async {
        let (mut conn, _) = listener.accept().await.unwrap();
        let mut buf = Vec::new();
        while !buf.ends_with(b"\r\n\r\n") {
            buf.push(conn.read_u8().await.unwrap());
            assert!(buf.len() < 2048);
        }
        let request = String::from_utf8(buf).unwrap().to_lowercase();
        assert!(request.starts_with("get /v1/cameras/cam_one/detections http/1.1\r\n"));
        assert!(request.contains("\r\nauthorization: bearer fixture\r\n"));
        conn.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
            .await
            .unwrap();
        s["valid_for_ms"] = json!(250);
        conn.write_all(&record(&s)).await.unwrap();
        // Start the next event. Its partial data cannot postpone the first deadline.
        let prefix = b"event: ";
        conn.write_all(prefix).await.unwrap();
        time::timeout(Duration::from_secs(2), async {
            while observed.borrow().len() < 3 {
                time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
        assert_eq!(
            &*observed.borrow(),
            &[
                Presence::Unavailable,
                Presence::Person,
                Presence::Unavailable
            ]
        );
        s["valid_for_ms"] = json!(1500);
        s["revision"] = json!(6);
        conn.write_all(&record(&s)[prefix.len()..]).await.unwrap();
        time::timeout(Duration::from_secs(1), async {
            while observed.borrow().len() < 4 {
                time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
        // Leave the server socket open until the caller cancels its subscription.
        conn
    };
    let _socket = tokio::select! {
        _ = feed.run(|p| observed.borrow_mut().push(p)) => unreachable!(),
        conn = server => conn,
    };
    assert_eq!(
        &observed.borrow()[3..],
        &[Presence::Person, Presence::Unavailable]
    );
}
