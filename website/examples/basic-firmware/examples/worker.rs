use std::error::Error;
use std::io;
use std::sync::mpsc;
use std::thread;

#[derive(Debug)]
enum Event {
    Add(u32),
    Stop,
}

fn process(inbox: std::sync::mpsc::Receiver<Event>) -> u64 {
    let mut total = 0;
    for event in inbox {
        match event {
            Event::Add(value) => total += u64::from(value),
            Event::Stop => break,
        }
    }
    total
}

fn main() -> Result<(), Box<dyn Error>> {
    let (sender, inbox) = mpsc::sync_channel(4);
    let worker = thread::Builder::new()
        .name("counter".into())
        .stack_size(32 * 1024)
        .spawn(move || process(inbox))?;

    let delivery = (|| -> Result<(), mpsc::SendError<Event>> {
        for value in [1, 2, 3] {
            sender.send(Event::Add(value))?;
        }
        sender.send(Event::Stop)
    })();

    // Also let the worker finish if sending a command failed.
    drop(sender);
    let total = worker
        .join()
        .map_err(|_| io::Error::other("counter worker panicked"))?;
    delivery?;
    println!("Processed total: {total}");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stop_finishes_after_admitted_updates() {
        let (sender, inbox) = mpsc::sync_channel(4);
        sender.send(Event::Add(2)).unwrap();
        sender.send(Event::Add(5)).unwrap();
        sender.send(Event::Stop).unwrap();
        sender.send(Event::Add(100)).unwrap();
        assert_eq!(process(inbox), 7);
    }

    #[test]
    fn disconnected_inbox_drains_then_finishes() {
        let (sender, inbox) = mpsc::sync_channel(1);
        sender.send(Event::Add(3)).unwrap();
        drop(sender);
        assert_eq!(process(inbox), 3);
    }

    #[test]
    fn empty_disconnected_inbox_finishes() {
        let (sender, inbox) = mpsc::sync_channel(1);
        drop(sender);
        assert_eq!(process(inbox), 0);
    }
}
