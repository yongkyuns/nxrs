use crate::Event;
use std::sync::mpsc::{RecvTimeoutError, TryRecvError, TrySendError};
use std::time::Duration;

#[derive(Debug, PartialEq, Eq)]
pub enum ReceiveError {
    Empty,
    Timeout,
    Closed,
}

pub trait Transport {
    type Sender: Send;
    type Receiver: Send + 'static;
    const NAME: &'static str;
    const SERVICE_QUEUES: usize;

    fn bounded(capacity: usize) -> (Self::Sender, Self::Receiver);
    fn send(sender: &Self::Sender, event: Event) -> Result<(), Event>;
    fn try_receive(receiver: &Self::Receiver) -> Result<Event, ReceiveError>;
    fn wait(receivers: &[Self::Receiver], timeout: Duration) -> Result<Event, ReceiveError>;
}

pub struct StdInbox;
impl Transport for StdInbox {
    type Sender = messaging::Sender<Event>;
    type Receiver = messaging::Inbox<Event>;
    const NAME: &'static str = "nxrs-std-inbox";
    const SERVICE_QUEUES: usize = 1;

    fn bounded(capacity: usize) -> (Self::Sender, Self::Receiver) {
        messaging::bounded(capacity)
    }
    fn send(sender: &Self::Sender, event: Event) -> Result<(), Event> {
        match sender.try_send(event) {
            Ok(()) => Ok(()),
            Err(TrySendError::Full(event)) => Err(event),
            Err(TrySendError::Disconnected(_)) => panic!("sender unexpectedly disconnected"),
        }
    }
    fn try_receive(receiver: &Self::Receiver) -> Result<Event, ReceiveError> {
        receiver.try_recv().map_err(|error| match error {
            TryRecvError::Empty => ReceiveError::Empty,
            TryRecvError::Disconnected => ReceiveError::Closed,
        })
    }
    fn wait(receivers: &[Self::Receiver], timeout: Duration) -> Result<Event, ReceiveError> {
        // Std Inbox has one owner wait point, not a multi-receiver selector.
        assert_eq!(receivers.len(), 1);
        receivers[0]
            .wait(Some(timeout))
            .map_err(|error| match error {
                RecvTimeoutError::Timeout => ReceiveError::Timeout,
                RecvTimeoutError::Disconnected => ReceiveError::Closed,
            })
    }
}

pub struct CrossbeamSelect;
impl Transport for CrossbeamSelect {
    type Sender = crossbeam_channel::Sender<Event>;
    type Receiver = crossbeam_channel::Receiver<Event>;
    const NAME: &'static str = "crossbeam-select-candidate";
    const SERVICE_QUEUES: usize = 3;

    fn bounded(capacity: usize) -> (Self::Sender, Self::Receiver) {
        crossbeam_channel::bounded(capacity)
    }
    fn send(sender: &Self::Sender, event: Event) -> Result<(), Event> {
        match sender.try_send(event) {
            Ok(()) => Ok(()),
            Err(crossbeam_channel::TrySendError::Full(event)) => Err(event),
            Err(crossbeam_channel::TrySendError::Disconnected(_)) => {
                panic!("sender unexpectedly disconnected")
            }
        }
    }
    fn try_receive(receiver: &Self::Receiver) -> Result<Event, ReceiveError> {
        receiver.try_recv().map_err(|error| match error {
            crossbeam_channel::TryRecvError::Empty => ReceiveError::Empty,
            crossbeam_channel::TryRecvError::Disconnected => ReceiveError::Closed,
        })
    }
    fn wait(receivers: &[Self::Receiver], timeout: Duration) -> Result<Event, ReceiveError> {
        assert_eq!(receivers.len(), 3);
        crossbeam_channel::select_biased! {
            recv(receivers[0]) -> value => value.map_err(|_| ReceiveError::Closed),
            recv(receivers[1]) -> value => value.map_err(|_| ReceiveError::Closed),
            recv(receivers[2]) -> value => value.map_err(|_| ReceiveError::Closed),
            default(timeout) => Err(ReceiveError::Timeout),
        }
    }
}
