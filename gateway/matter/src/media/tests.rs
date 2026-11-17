use super::*;

fn frame(tick: u64, keyframe: bool) -> Message {
    Message::Picture(
        1,
        AccessUnit {
            pts_90khz: tick,
            keyframe,
            dimensions: (640, 360),
            annex_b: Bytes::from_static(&[0, 0, 0, 1, 0x65]),
        },
    )
}

#[tokio::test]
async fn slow_peer_resumes_on_new_keyframe_and_retired_frames_are_inaccessible() {
    let sender = broadcast::Sender::new(2);
    let current = Arc::new(AtomicU64::new(1));
    let video = Video {
        sender: sender.downgrade(),
        current: current.clone(),
    };
    let mut fast = video.subscribe().unwrap();
    let mut slow = video.subscribe().unwrap();
    sender.send(frame(0, true)).ok().unwrap();
    assert_eq!(fast.next().await.unwrap().pts_90khz, 0);
    assert_eq!(slow.next().await.unwrap().pts_90khz, 0);
    for tick in 1..=4 {
        sender.send(frame(tick, false)).ok().unwrap();
        assert_eq!(fast.next().await.unwrap().pts_90khz, tick);
    }
    // Receiver lag and a partial GOP must not produce undecodable pictures.
    assert!(time::timeout(Duration::from_millis(5), slow.next())
        .await
        .is_err());
    sender.send(frame(5, false)).ok().unwrap();
    sender.send(frame(6, true)).ok().unwrap();
    assert_eq!(slow.next().await.unwrap().pts_90khz, 6);
    current.store(2, Ordering::Release);
    assert!(fast.next().await.is_none());
    assert!(slow.next().await.is_none());
}

#[tokio::test(start_paused = true)]
async fn an_offline_source_cannot_keep_an_opening_peer_alive() {
    let sender = broadcast::Sender::new(2);
    let video = Video {
        sender: sender.downgrade(),
        current: Arc::new(AtomicU64::new(0)),
    };
    let mut peer = video.subscribe().unwrap();
    let start = Instant::now();
    let abandon = async {
        for _ in 0..3 {
            sender.send(Message::End).ok().unwrap();
            assert!(time::timeout(Duration::from_secs(2), peer.next())
                .await
                .is_err());
        }
        peer.next().await
    };
    assert!(abandon.await.is_none());
    assert_eq!(Instant::now() - start, STARTUP);
}
