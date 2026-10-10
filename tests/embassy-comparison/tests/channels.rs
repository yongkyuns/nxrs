use std::future::{Future, poll_fn};
use std::pin::{Pin, pin};
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::task::{Context, Poll, Wake, Waker};

use embassy_sync::blocking_mutex::raw::CriticalSectionRawMutex;
use embassy_sync::channel::Channel;
use nxrs_embassy_comparison::messaging::receive_any;

const MAX_POLLS: usize = 100_000;

#[derive(Default)]
struct WakeCounter(AtomicUsize);

impl Wake for WakeCounter {
    fn wake(self: Arc<Self>) {
        self.wake_by_ref();
    }

    fn wake_by_ref(self: &Arc<Self>) {
        self.0.fetch_add(1, Ordering::SeqCst);
    }
}

fn test_waker() -> (Arc<WakeCounter>, Waker) {
    let counter = Arc::new(WakeCounter::default());
    let waker = Waker::from(Arc::clone(&counter));
    (counter, waker)
}

/// Drive a future cooperatively, with a hard bound so a lost wake or deadlock
/// fails the test instead of hanging the test process.
fn run_to_completion<F: Future>(future: F) -> F::Output {
    let (_, waker) = test_waker();
    let mut context = Context::from_waker(&waker);
    let mut future = pin!(future);

    for _ in 0..MAX_POLLS {
        if let Poll::Ready(output) = future.as_mut().poll(&mut context) {
            return output;
        }
    }

    panic!("future did not complete within {MAX_POLLS} polls");
}

type TestChannel<T, const N: usize> = Channel<CriticalSectionRawMutex, T, N>;

fn receive_round_robin<'a, const N: usize>(
    queues: &'a [TestChannel<u8, 1>; N],
    next_queue: &'a mut usize,
) -> impl Future<Output = (usize, u8)> + 'a {
    receive_any(queues, next_queue)
}

#[test]
fn concurrent_senders_preserve_each_stream_across_backpressure() {
    const SENDERS: usize = 4;
    const MESSAGES: usize = 32;

    let channel = TestChannel::<(usize, usize), 1>::new();

    let senders: Vec<Pin<Box<dyn Future<Output = ()>>>> = (0..SENDERS)
        .map(|sender_id| {
            let sender = channel.sender();
            Box::pin(async move {
                for sequence in 0..MESSAGES {
                    sender.send((sender_id, sequence)).await;
                }
            }) as Pin<Box<dyn Future<Output = ()>>>
        })
        .collect();
    let mut senders = senders;
    let mut finished = [false; SENDERS];
    let mut received = [0; SENDERS];
    run_to_completion(poll_fn(|cx| {
        for (index, sender) in senders.iter_mut().enumerate() {
            if !finished[index] && sender.as_mut().poll(cx).is_ready() {
                finished[index] = true;
            }
        }

        if received.iter().sum::<usize>() == SENDERS * MESSAGES {
            assert!(finished.iter().all(|is_finished| *is_finished));
            return Poll::Ready(());
        }

        if let Poll::Ready((sender_id, sequence)) = channel.poll_receive(cx) {
            assert!(sender_id < SENDERS, "unexpected sender id {sender_id}");
            assert_eq!(
                sequence, received[sender_id],
                "sender {sender_id} stream was reordered"
            );
            received[sender_id] += 1;
        }

        Poll::Pending
    }));

    assert_eq!(received, [MESSAGES; SENDERS]);
}

#[test]
fn round_robin_multi_queue_receive_registers_and_wakes_non_first_queue() {
    let queues = [
        TestChannel::<u8, 1>::new(),
        TestChannel::<u8, 1>::new(),
        TestChannel::<u8, 1>::new(),
    ];
    let mut next_queue = 0;
    let (wake_counter, waker) = test_waker();
    let mut context = Context::from_waker(&waker);

    {
        let receive_any = receive_round_robin(&queues, &mut next_queue);
        let mut receive_any = pin!(receive_any);

        assert!(receive_any.as_mut().poll(&mut context).is_pending());
        assert!(queues.iter().all(Channel::is_empty));

        queues[2].try_send(42).expect("queue 2 should have room");
        assert!(wake_counter.0.load(Ordering::SeqCst) > 0);
        assert_eq!(
            receive_any.as_mut().poll(&mut context),
            Poll::Ready((2, 42))
        );
    }

    // All queues can be reused, and selection advances from the last winner.
    queues[0].try_send(10).unwrap();
    queues[1].try_send(11).unwrap();
    queues[2].try_send(12).unwrap();
    assert_eq!(
        run_to_completion(receive_round_robin(&queues, &mut next_queue)),
        (0, 10)
    );
    assert_eq!(
        run_to_completion(receive_round_robin(&queues, &mut next_queue)),
        (1, 11)
    );
    assert_eq!(
        run_to_completion(receive_round_robin(&queues, &mut next_queue)),
        (2, 12)
    );
}

#[test]
fn capacity_one_send_waits_for_space_and_channel_is_reusable() {
    let channel = TestChannel::<u8, 1>::new();

    run_to_completion(async {
        channel.send(1).await;
        assert!(channel.is_full());

        let second_send = channel.send(2);
        let mut second_send = pin!(second_send);
        let was_blocked =
            poll_fn(|cx| Poll::Ready(second_send.as_mut().poll(cx).is_pending())).await;
        assert!(
            was_blocked,
            "send into a full capacity-one channel must wait"
        );

        assert_eq!(channel.receive().await, 1);
        second_send.as_mut().await;
        assert_eq!(channel.receive().await, 2);

        channel.send(3).await;
        assert_eq!(channel.receive().await, 3);
        assert!(channel.is_empty());
    });
}

#[test]
fn distinct_sender_wakers_complete_without_unconditional_repolling() {
    let channel = TestChannel::<(usize, usize), 1>::new();
    let mut senders: Vec<Pin<Box<dyn Future<Output = ()>>>> = (0..4)
        .map(|id| {
            let channel = &channel;
            Box::pin(async move {
                for sequence in 0..32 {
                    channel.send((id, sequence)).await;
                }
            }) as Pin<Box<dyn Future<Output = ()>>>
        })
        .collect();
    let sender_wakers: Vec<_> = (0..4).map(|_| test_waker()).collect();
    let mut finished = [false; 4];
    let mut received = [0; 4];
    let receiver = async {
        for _ in 0..128 {
            let (id, sequence) = channel.receive().await;
            assert_eq!(sequence, received[id]);
            received[id] += 1;
        }
    };
    let mut receiver = pin!(receiver);
    let (receiver_counter, receiver_waker) = test_waker();
    let mut receiver_finished = false;
    for iteration in 0..MAX_POLLS {
        for id in 0..4 {
            let (counter, waker) = &sender_wakers[id];
            if !finished[id] && (iteration == 0 || counter.0.swap(0, Ordering::SeqCst) > 0) {
                finished[id] = senders[id]
                    .as_mut()
                    .poll(&mut Context::from_waker(waker))
                    .is_ready();
            }
        }
        if !receiver_finished
            && (iteration == 0 || receiver_counter.0.swap(0, Ordering::SeqCst) > 0)
        {
            receiver_finished = receiver
                .as_mut()
                .poll(&mut Context::from_waker(&receiver_waker))
                .is_ready();
        }
        if receiver_finished && finished.iter().all(|&done| done) {
            return;
        }
    }
    panic!("distinct-waker sender/receiver deadlock");
}
