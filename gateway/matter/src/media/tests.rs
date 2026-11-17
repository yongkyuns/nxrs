use super::*;

fn frame(tick: u64, keyframe: bool) -> Message {
    Message::Picture(
        1,
        AccessUnit {
            pts_90khz: tick,
            source_time: std::time::Instant::now(),
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
        sound: broadcast::Sender::new(2).downgrade(),
        opus_generation: Arc::new(AtomicU64::new(0)),
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
        sound: broadcast::Sender::new(2).downgrade(),
        opus_generation: Arc::new(AtomicU64::new(0)),
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

#[tokio::test]
async fn microphone_queue_skips_lag_and_cannot_deliver_retired_audio() {
    let sound = broadcast::Sender::new(2);
    let current = Arc::new(AtomicU64::new(1));
    let video = Video {
        sender: broadcast::Sender::new(1).downgrade(),
        sound: sound.downgrade(),
        current: current.clone(),
        opus_generation: Arc::new(AtomicU64::new(1)),
    };
    let mut audio = video.subscribe_audio().unwrap();
    let send = |tick| {
        let _ = sound.send(Some((
            1,
            AudioPacket {
                pts_48khz: tick,
                source_time: std::time::Instant::now(),
                opus: Bytes::from_static(&[1]),
            },
        )));
    };
    for tick in 0..3 {
        send(tick);
    }
    tokio::join!(
        async { assert_eq!(audio.next().await.unwrap().pts_48khz, 3) },
        async {
            tokio::task::yield_now().await;
            send(3)
        }
    );
    send(4);
    current.store(2, Ordering::Release);
    assert!(audio.next().await.is_none());
    assert!(video.subscribe_audio().is_none());
}
