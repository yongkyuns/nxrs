//! Statically dispatched calls to the actual transports under measurement.
//!
//! Three-arm selection preserves the Wi-Fi fixture's stop/important/ordinary
//! order, but measures only its ordinary arm. The two idle admission classes
//! stay connected. This isolates a selection case; it is not the P3 owner,
//! fairness, deadline, source-fault or shutdown qualification.
#![forbid(unsafe_code)]

use std::{sync::mpsc, time::Duration};

use crate::payload::Payload;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Admission<P> {
    Full(P),
    Closed(P),
}

pub trait SendEndpoint<P: Payload>: Clone + Send + 'static {
    fn send(&self, payload: P) -> Result<(), Admission<P>>;
    fn try_send(&self, payload: P) -> Result<(), Admission<P>>;
}

pub trait ReceiveEndpoint<P: Payload>: Send + 'static {
    fn recv(&mut self) -> Result<P, &'static str>;
    fn try_recv(&mut self) -> Result<Option<P>, &'static str>;
    fn recv_timeout(&mut self, timeout: Duration) -> Result<Option<P>, &'static str>;
}

pub trait Transport {
    const NAME: &'static str;
    /// Number of receives at the wait point; independent of provider count.
    const ARMS: usize;
    type Tx<P: Payload>: SendEndpoint<P>;
    type Rx<P: Payload>: ReceiveEndpoint<P>;

    fn channel<P: Payload>(capacity: usize) -> (Self::Tx<P>, Self::Rx<P>);
}

pub struct RawStd;
pub struct Messaging;
pub struct CrossbeamDirect;
pub struct CrossbeamSelectOne;
pub struct CrossbeamSelectThree;

impl Transport for RawStd {
    const NAME: &'static str = "raw-std";
    const ARMS: usize = 1;
    type Tx<P: Payload> = mpsc::SyncSender<P>;
    type Rx<P: Payload> = mpsc::Receiver<P>;

    fn channel<P: Payload>(capacity: usize) -> (Self::Tx<P>, Self::Rx<P>) {
        assert!(capacity > 0, "P2 measures bounded positive-capacity queues");
        mpsc::sync_channel(capacity)
    }
}

impl<P: Payload> SendEndpoint<P> for mpsc::SyncSender<P> {
    fn send(&self, payload: P) -> Result<(), Admission<P>> {
        mpsc::SyncSender::send(self, payload).map_err(|error| Admission::Closed(error.0))
    }

    fn try_send(&self, payload: P) -> Result<(), Admission<P>> {
        mpsc::SyncSender::try_send(self, payload).map_err(std_admission)
    }
}

fn std_admission<P>(error: mpsc::TrySendError<P>) -> Admission<P> {
    match error {
        mpsc::TrySendError::Full(value) => Admission::Full(value),
        mpsc::TrySendError::Disconnected(value) => Admission::Closed(value),
    }
}

fn std_try_receive<P>(result: Result<P, mpsc::TryRecvError>) -> Result<Option<P>, &'static str> {
    match result {
        Ok(value) => Ok(Some(value)),
        Err(mpsc::TryRecvError::Empty) => Ok(None),
        Err(mpsc::TryRecvError::Disconnected) => Err("channel-closed"),
    }
}

fn std_timed_receive<P>(
    result: Result<P, mpsc::RecvTimeoutError>,
) -> Result<Option<P>, &'static str> {
    match result {
        Ok(value) => Ok(Some(value)),
        Err(mpsc::RecvTimeoutError::Timeout) => Ok(None),
        Err(mpsc::RecvTimeoutError::Disconnected) => Err("channel-closed"),
    }
}

impl<P: Payload> ReceiveEndpoint<P> for mpsc::Receiver<P> {
    fn recv(&mut self) -> Result<P, &'static str> {
        mpsc::Receiver::recv(self).map_err(|_| "channel-closed")
    }

    fn try_recv(&mut self) -> Result<Option<P>, &'static str> {
        std_try_receive(mpsc::Receiver::try_recv(self))
    }

    fn recv_timeout(&mut self, timeout: Duration) -> Result<Option<P>, &'static str> {
        std_timed_receive(mpsc::Receiver::recv_timeout(self, timeout))
    }
}

impl Transport for Messaging {
    const NAME: &'static str = "messaging";
    const ARMS: usize = 1;
    type Tx<P: Payload> = messaging::Sender<P>;
    type Rx<P: Payload> = messaging::Inbox<P>;

    fn channel<P: Payload>(capacity: usize) -> (Self::Tx<P>, Self::Rx<P>) {
        messaging::bounded(capacity)
    }
}

impl<P: Payload> SendEndpoint<P> for messaging::Sender<P> {
    fn send(&self, payload: P) -> Result<(), Admission<P>> {
        messaging::Sender::send(self, payload).map_err(|error| Admission::Closed(error.0))
    }

    fn try_send(&self, payload: P) -> Result<(), Admission<P>> {
        messaging::Sender::try_send(self, payload).map_err(std_admission)
    }
}

impl<P: Payload> ReceiveEndpoint<P> for messaging::Inbox<P> {
    fn recv(&mut self) -> Result<P, &'static str> {
        self.wait(None).map_err(|_| "channel-closed")
    }

    fn try_recv(&mut self) -> Result<Option<P>, &'static str> {
        std_try_receive(messaging::Inbox::try_recv(self))
    }

    fn recv_timeout(&mut self, timeout: Duration) -> Result<Option<P>, &'static str> {
        std_timed_receive(self.wait(Some(timeout)))
    }
}

impl Transport for CrossbeamDirect {
    const NAME: &'static str = "crossbeam-direct";
    const ARMS: usize = 1;
    type Tx<P: Payload> = crossbeam_channel::Sender<P>;
    type Rx<P: Payload> = crossbeam_channel::Receiver<P>;

    fn channel<P: Payload>(capacity: usize) -> (Self::Tx<P>, Self::Rx<P>) {
        assert!(capacity > 0, "P2 measures bounded positive-capacity queues");
        crossbeam_channel::bounded(capacity)
    }
}

impl<P: Payload> SendEndpoint<P> for crossbeam_channel::Sender<P> {
    fn send(&self, payload: P) -> Result<(), Admission<P>> {
        crossbeam_channel::Sender::send(self, payload).map_err(|error| Admission::Closed(error.0))
    }

    fn try_send(&self, payload: P) -> Result<(), Admission<P>> {
        crossbeam_channel::Sender::try_send(self, payload).map_err(|error| match error {
            crossbeam_channel::TrySendError::Full(value) => Admission::Full(value),
            crossbeam_channel::TrySendError::Disconnected(value) => Admission::Closed(value),
        })
    }
}

impl<P: Payload> ReceiveEndpoint<P> for crossbeam_channel::Receiver<P> {
    fn recv(&mut self) -> Result<P, &'static str> {
        crossbeam_channel::Receiver::recv(self).map_err(|_| "channel-closed")
    }

    fn try_recv(&mut self) -> Result<Option<P>, &'static str> {
        match crossbeam_channel::Receiver::try_recv(self) {
            Ok(value) => Ok(Some(value)),
            Err(crossbeam_channel::TryRecvError::Empty) => Ok(None),
            Err(crossbeam_channel::TryRecvError::Disconnected) => Err("channel-closed"),
        }
    }

    fn recv_timeout(&mut self, timeout: Duration) -> Result<Option<P>, &'static str> {
        match crossbeam_channel::Receiver::recv_timeout(self, timeout) {
            Ok(value) => Ok(Some(value)),
            Err(crossbeam_channel::RecvTimeoutError::Timeout) => Ok(None),
            Err(crossbeam_channel::RecvTimeoutError::Disconnected) => Err("channel-closed"),
        }
    }
}

pub struct OneReceiver<P> {
    ordinary: crossbeam_channel::Receiver<P>,
}

impl Transport for CrossbeamSelectOne {
    const NAME: &'static str = "crossbeam-select-one";
    const ARMS: usize = 1;
    type Tx<P: Payload> = crossbeam_channel::Sender<P>;
    type Rx<P: Payload> = OneReceiver<P>;

    fn channel<P: Payload>(capacity: usize) -> (Self::Tx<P>, Self::Rx<P>) {
        let (sender, ordinary) = CrossbeamDirect::channel(capacity);
        (sender, OneReceiver { ordinary })
    }
}

impl<P: Payload> ReceiveEndpoint<P> for OneReceiver<P> {
    fn recv(&mut self) -> Result<P, &'static str> {
        crossbeam_channel::select_biased! {
            recv(self.ordinary) -> value => value.map_err(|_| "channel-closed"),
        }
    }

    fn try_recv(&mut self) -> Result<Option<P>, &'static str> {
        crossbeam_channel::select_biased! {
            recv(self.ordinary) -> value => value.map(Some).map_err(|_| "channel-closed"),
            default => Ok(None),
        }
    }

    fn recv_timeout(&mut self, timeout: Duration) -> Result<Option<P>, &'static str> {
        crossbeam_channel::select_biased! {
            recv(self.ordinary) -> value => value.map(Some).map_err(|_| "channel-closed"),
            default(timeout) => Ok(None),
        }
    }
}

pub struct ThreeReceiver<P> {
    stop: crossbeam_channel::Receiver<()>,
    important: crossbeam_channel::Receiver<P>,
    ordinary: crossbeam_channel::Receiver<P>,
    // A disconnected idle arm would stay ready and invalidate the measurement.
    _stop_sender: crossbeam_channel::Sender<()>,
    _important_sender: crossbeam_channel::Sender<P>,
}

impl Transport for CrossbeamSelectThree {
    const NAME: &'static str = "crossbeam-select-three-ordinary";
    const ARMS: usize = 3;
    type Tx<P: Payload> = crossbeam_channel::Sender<P>;
    type Rx<P: Payload> = ThreeReceiver<P>;

    fn channel<P: Payload>(capacity: usize) -> (Self::Tx<P>, Self::Rx<P>) {
        let (ordinary_sender, ordinary) = CrossbeamDirect::channel(capacity);
        let (_stop_sender, stop) = crossbeam_channel::bounded(1);
        let (_important_sender, important) = crossbeam_channel::bounded(capacity);
        (
            ordinary_sender,
            ThreeReceiver {
                stop,
                important,
                ordinary,
                _stop_sender,
                _important_sender,
            },
        )
    }
}

impl<P: Payload> ReceiveEndpoint<P> for ThreeReceiver<P> {
    fn recv(&mut self) -> Result<P, &'static str> {
        crossbeam_channel::select_biased! {
            recv(self.stop) -> _value => Err("stop-arm-ready"),
            recv(self.important) -> value => value.map_err(|_| "important-arm-closed"),
            recv(self.ordinary) -> value => value.map_err(|_| "channel-closed"),
        }
    }

    fn try_recv(&mut self) -> Result<Option<P>, &'static str> {
        crossbeam_channel::select_biased! {
            recv(self.stop) -> _value => Err("stop-arm-ready"),
            recv(self.important) -> value => value.map(Some).map_err(|_| "important-arm-closed"),
            recv(self.ordinary) -> value => value.map(Some).map_err(|_| "channel-closed"),
            default => Ok(None),
        }
    }

    fn recv_timeout(&mut self, timeout: Duration) -> Result<Option<P>, &'static str> {
        crossbeam_channel::select_biased! {
            recv(self.stop) -> _value => Err("stop-arm-ready"),
            recv(self.important) -> value => value.map(Some).map_err(|_| "important-arm-closed"),
            recv(self.ordinary) -> value => value.map(Some).map_err(|_| "channel-closed"),
            default(timeout) => Ok(None),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::payload::Bytes16;

    fn admission_and_order<T: Transport, P: Payload>() {
        let (sender, mut receiver) = T::channel::<P>(8);
        assert_eq!(receiver.try_recv(), Ok(None));
        assert_eq!(receiver.recv_timeout(Duration::ZERO), Ok(None));
        let clone = sender.clone();
        for sequence in 0..8 {
            sender.try_send(P::new(sequence, 1)).unwrap();
        }
        let rejected = P::new(8, 1);
        assert_eq!(clone.try_send(rejected), Err(Admission::Full(rejected)));
        for sequence in 0..8 {
            assert_eq!(receiver.recv().unwrap(), P::new(sequence, 1));
        }
        assert_eq!(receiver.try_recv(), Ok(None));
        clone.send(rejected).unwrap();
        drop(sender);
        drop(clone);
        assert_eq!(receiver.recv().unwrap(), rejected);
        assert_eq!(receiver.recv(), Err("channel-closed"));
        assert_eq!(receiver.try_recv(), Err("channel-closed"));
        assert_eq!(receiver.recv_timeout(Duration::ZERO), Err("channel-closed"));

        let (sender, receiver) = T::channel::<P>(8);
        drop(receiver);
        assert_eq!(sender.send(rejected), Err(Admission::Closed(rejected)));
        assert_eq!(sender.try_send(rejected), Err(Admission::Closed(rejected)));
    }

    fn exercise<T: Transport>() {
        admission_and_order::<T, Bytes16>();
        admission_and_order::<T, imu::ImuSample>();
    }

    #[test]
    fn raw_std_uses_checked_positive_capacity_admission_and_fifo() {
        exercise::<RawStd>();
    }

    #[test]
    fn actual_messaging_facade_preserves_identical_admission_and_fifo() {
        exercise::<Messaging>();
    }

    #[test]
    fn pinned_crossbeam_direct_preserves_admission_and_fifo() {
        exercise::<CrossbeamDirect>();
    }

    #[test]
    fn one_arm_selection_preserves_admission_and_fifo() {
        exercise::<CrossbeamSelectOne>();
    }

    #[test]
    fn three_arm_selection_keeps_idle_arms_connected_and_preserves_fifo() {
        exercise::<CrossbeamSelectThree>();
    }

    #[test]
    fn three_arm_priority_does_not_consume_unselected_ordinary_event() {
        let (sender, mut receiver) = CrossbeamSelectThree::channel::<Bytes16>(8);
        let ordinary = Bytes16::new(0, 0);
        let important = Bytes16::new(0, 1);
        sender.try_send(ordinary).unwrap();
        receiver._important_sender.try_send(important).unwrap();
        assert_eq!(receiver.recv(), Ok(important));
        assert_eq!(receiver.recv(), Ok(ordinary));
        assert_eq!(receiver.try_recv(), Ok(None));
    }

    #[test]
    fn three_arm_stop_admission_is_independent_of_full_ordinary_queue() {
        let (sender, mut receiver) = CrossbeamSelectThree::channel::<Bytes16>(1);
        let ordinary = Bytes16::new(0, 0);
        sender.try_send(ordinary).unwrap();
        assert!(matches!(
            sender.try_send(Bytes16::new(1, 0)),
            Err(crossbeam_channel::TrySendError::Full(_))
        ));
        receiver._stop_sender.try_send(()).unwrap();
        assert_eq!(receiver.recv(), Err("stop-arm-ready"));
        assert_eq!(receiver.recv(), Ok(ordinary));
    }

    #[test]
    fn selected_receiver_waits_for_a_later_message() {
        let (sender, mut receiver) = CrossbeamSelectThree::channel::<Bytes16>(8);
        let value = Bytes16::new(73, 2);
        let worker = std::thread::spawn(move || receiver.recv_timeout(Duration::from_secs(2)));
        // This tests delivery and cleanup, not independent proof of a block.
        sender.send(value).unwrap();
        assert_eq!(worker.join().unwrap(), Ok(Some(value)));
    }
}
